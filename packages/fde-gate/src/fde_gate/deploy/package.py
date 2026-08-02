"""Build the gate service Lambda deployment zip.

Three steps, and the awkward part of each one is load-bearing:

1. `uv export --no-emit-workspace` writes a requirements file of THIRD-PARTY
   pins only. Workspace members are excluded because they are not on any
   index -- they are copied from the source tree in step 3.
2. `uv pip install --target` with `--python-platform aarch64-manylinux_2_28`
   and `--only-binary :all:`. The platform flag is mandatory, not an
   optimisation: `psycopg[binary]` and `pydantic` ship compiled wheels, and a
   zip built on an x86 laptop without it installs x86 binaries that import
   fine locally and fail in the arm64 runtime with a bare
   "No module named 'psycopg_binary'". `--only-binary` turns "there is no
   arm64 wheel for this" into a build failure rather than an sdist that gets
   compiled for the wrong architecture.

   `manylinux_2_28`, not `manylinux2014`: psycopg-binary publishes aarch64
   wheels only as `manylinux_2_27_aarch64` / `manylinux_2_28_aarch64`, so
   the 2014 tag resolves to nothing and the build fails outright (verified).
   The Lambda python3.12 runtime is Amazon Linux 2023, glibc 2.34, so
   2.28 wheels are within its floor with room to spare.
3. `fde_gate` and `fde_mcp` are copied from `packages/*/src/`, templates
   included -- the console renders `TemplateNotFound` on its first request
   without them, which is a 500 no amount of Lambda configuration explains.

`uv` throughout, no `pip`. docs/11 §1 makes that a rule for the whole repo,
and a deployment artifact resolved by a different resolver than CI tested is
exactly the drift the rule exists to prevent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Directories that are never worth shipping: they are large, they are
# rebuildable, and __pycache__ from a different Python would shadow the real
# modules at import time.
_PRUNE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_PRUNE_SUFFIXES = (".pyc", ".pyo", ".dist-info/RECORD")

_WORKSPACE_PACKAGES = (
    ("fde-gate", "fde_gate"),
    ("fde-mcp", "fde_mcp"),
)

# Must match `lambda_fn.DEFAULT_ARCHITECTURE`. A mismatch between what this
# builds for and what the function is created as is not a build failure --
# it is an ImportError on the first request, which is a much worse place to
# find out. See the module docstring for why the glibc floor is 2.28.
LAMBDA_PYTHON_PLATFORM = "aarch64-manylinux_2_28"


def _repo_root() -> Path:
    """The workspace root, found by walking up for the marker that defines it.

    Not `parents[N]`: an index is off by one the moment anything moves, and
    the failure would not look like a wrong path -- `uv export` run one
    directory too low resolves a DIFFERENT dependency set and the zip is
    quietly wrong. Not `cwd` either, since an operator running this from
    anywhere but the repo root is the normal case.
    """
    for candidate in Path(__file__).resolve().parents:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "[tool.uv.workspace]" in pyproject.read_text():
            return candidate
    msg = (
        "cannot find the uv workspace root above "
        f"{Path(__file__).resolve()}; run this from a source checkout, not "
        "from an installed wheel"
    )
    raise FileNotFoundError(msg)


def _run(command: list[str], cwd: Path) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def _export_requirements(root: Path, build_dir: Path, *, frozen: bool) -> Path:
    requirements = build_dir / "requirements.txt"
    command = [
        "uv",
        "export",
        "--package",
        "fde-gate",
        "--no-dev",
        "--no-emit-workspace",
        "--no-hashes",
        "-o",
        str(requirements),
    ]
    if frozen:
        command.append("--frozen")
    _run(command, cwd=root)
    return requirements


def _install_dependencies(root: Path, requirements: Path, site: Path) -> None:
    _run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(site),
            "--python-version",
            "3.12",
            "--python-platform",
            LAMBDA_PYTHON_PLATFORM,
            "--only-binary",
            ":all:",
            "-r",
            str(requirements),
        ],
        cwd=root,
    )


def _copy_sources(root: Path, site: Path) -> None:
    for distribution, module in _WORKSPACE_PACKAGES:
        source = root / "packages" / distribution / "src" / module
        if not source.is_dir():
            msg = f"expected package source at {source}"
            raise FileNotFoundError(msg)
        destination = site / module
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*_PRUNE_DIRS, "*.pyc"),
        )
        print(f"  copied {module} from {source}")


def _write_zip(site: Path, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(site.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _PRUNE_DIRS for part in path.parts):
                continue
            if path.name.endswith(_PRUNE_SUFFIXES):
                continue
            archive.write(path, path.relative_to(site))
            count += 1
    return count


def build(out: Path, *, build_dir: Path, frozen: bool = True) -> Path:
    root = _repo_root()
    site = build_dir / "site"
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)

    requirements = _export_requirements(root, build_dir, frozen=frozen)
    _install_dependencies(root, requirements, site)
    _copy_sources(root, site)
    files = _write_zip(site, out)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"wrote {out} ({files} files, {size_mb:.1f} MiB)")
    if size_mb > 50:
        print(
            "warning: over Lambda's 50 MiB direct-upload limit; upload via S3 "
            "(`fde-gate-deploy lambda --s3-bucket ... --s3-key ...`).",
            file=sys.stderr,
        )
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=Path("dist/fde-gate.zip"))
    p.add_argument("--build-dir", type=Path, default=Path("build/fde-gate"))
    p.add_argument(
        "--no-frozen",
        action="store_true",
        help="resolve fresh instead of using uv.lock; only for debugging a lock problem",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.build_dir.mkdir(parents=True, exist_ok=True)
    try:
        build(args.out, build_dir=args.build_dir, frozen=not args.no_frozen)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"package build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
