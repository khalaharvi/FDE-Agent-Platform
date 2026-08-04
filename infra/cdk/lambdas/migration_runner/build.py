"""Build the migration-runner Lambda deployment zip.

Bundles `handler.py`, a copy of the repo's `db/0*.sql` migrations, and
`psycopg[binary]` wheels built for the Lambda's runtime (python3.12,
arm64). The `--python-platform aarch64-manylinux_2_28 --only-binary :all:`
flags are the exact, hard-won pattern from
`packages/fde-gate/src/fde_gate/deploy/package.py::_install_dependencies`
(read it first): `psycopg[binary]` only ships `manylinux_2_27`/
`manylinux_2_28` aarch64 wheels (no `manylinux2014`), and building without
`--python-platform` on anything but an arm64 Linux box installs the WRONG
architecture's compiled extension -- it imports fine locally and fails in
the Lambda runtime with a bare `No module named 'psycopg_binary'`.

Unlike `fde_gate/deploy/package.py`, there is no `uv export` step here:
this Lambda is not a uv workspace package (`infra/cdk` is its own
standalone `uv` project, not a member of the repo-root workspace -- see
`infra/cdk/pyproject.toml`'s `[tool.uv] package = false`), so there is no
`pyproject.toml`/lockfile of its own to export requirements from.
`PSYCOPG_SPEC` below is this build's single source of truth for the
version installed, pinned to match what `uv.lock` already resolves for the
rest of the repo (`psycopg==3.3.4`) rather than re-resolving against
`psycopg[binary]>=3.2`'s open range and silently drifting from the version
the rest of the platform is tested against.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Directories/files that are never worth shipping -- see
# `fde_gate/deploy/package.py` for the identical rationale.
_PRUNE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_PRUNE_SUFFIXES = (".pyc", ".pyo", ".dist-info/RECORD")
# `uv pip install --target` (unlike a normal project sync) drops a 0-byte
# `.lock` marker at the target root to serialize concurrent writes to it --
# harmless in the zip (nothing imports it) but not worth shipping either.
_PRUNE_NAMES = frozenset({".lock"})

# Must match `fde_cdk/migrations.py`'s `lambda_.Architecture.ARM_64` /
# `lambda_.Runtime.PYTHON_3_12`. A mismatch here is not a build failure --
# it is an ImportError on the Lambda's first invocation.
LAMBDA_PYTHON_PLATFORM = "aarch64-manylinux_2_28"
LAMBDA_PYTHON_VERSION = "3.12"

# No `[pool]` extra: one connection per invocation, no cross-invocation
# pooling. Pinned, not `>=3.2`: see module docstring.
PSYCOPG_SPEC = "psycopg[binary]==3.3.4"

_HANDLER_FILE = Path(__file__).resolve().parent / "handler.py"


def _repo_root() -> Path:
    """Walk up for the uv WORKSPACE root (the repo root's own
    `pyproject.toml`, which has `[tool.uv.workspace]`) -- not `infra/cdk`'s
    own `pyproject.toml`, which is a standalone project without that
    marker. Same approach as `fde_gate/deploy/package.py._repo_root` for
    the same reason: an `parents[N]` index is off by one the moment
    anything moves, and the failure would not look like a wrong path --
    it would quietly copy the wrong `db/` directory.
    """
    for candidate in Path(__file__).resolve().parents:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "[tool.uv.workspace]" in pyproject.read_text():
            return candidate
    msg = (
        "cannot find the uv workspace root above "
        f"{Path(__file__).resolve()}; run this from a source checkout"
    )
    raise FileNotFoundError(msg)


def _run(command: list[str], cwd: Path) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def _install_dependencies(root: Path, site: Path) -> None:
    _run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(site),
            "--python-version",
            LAMBDA_PYTHON_VERSION,
            "--python-platform",
            LAMBDA_PYTHON_PLATFORM,
            "--only-binary",
            ":all:",
            PSYCOPG_SPEC,
        ],
        cwd=root,
    )


def _copy_handler(site: Path) -> None:
    shutil.copy2(_HANDLER_FILE, site / "handler.py")


def _copy_migrations(root: Path, site: Path) -> None:
    source = root / "db"
    destination = site / "db"
    destination.mkdir(parents=True, exist_ok=True)
    files = sorted(source.glob("0*.sql"))
    if not files:
        msg = f"no db/0*.sql migrations found under {source}"
        raise FileNotFoundError(msg)
    for path in files:
        shutil.copy2(path, destination / path.name)
    print(f"  copied {len(files)} migration(s) from {source}")


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
            if path.name.endswith(_PRUNE_SUFFIXES) or path.name in _PRUNE_NAMES:
                continue
            archive.write(path, path.relative_to(site))
            count += 1
    return count


def build(out: Path, *, build_dir: Path) -> Path:
    root = _repo_root()
    # Resolve to absolute paths BEFORE `_install_dependencies` runs `uv pip
    # install` with `cwd=root` (matching `fde_gate/deploy/package.py`'s own
    # `cwd=root` pattern): this build is invoked from `infra/cdk`
    # (`uv run python lambdas/migration_runner/build.py`), not from `root`.
    # A relative `--target build/migration-runner/site` would then resolve
    # against the SUBPROCESS's cwd (`root`), silently writing the installed
    # wheels to `<root>/build/...` while every other step below (`site.
    # mkdir`, `_copy_handler`, `_write_zip`) operates on the SAME relative
    # Path resolved against THIS process's cwd (`infra/cdk/build/...`) --
    # two different directories, so the zip would end up with `handler.py`
    # + the migrations but silently no `psycopg` at all (verified: this
    # exact bug produced a 16-file zip with zero third-party packages
    # before this `.resolve()` was added).
    build_dir = build_dir.resolve()
    out = out.resolve()
    site = build_dir / "site"
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)

    _install_dependencies(root, site)
    _copy_handler(site)
    _copy_migrations(root, site)
    files = _write_zip(site, out)

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"wrote {out} ({files} files, {size_mb:.1f} MiB)")
    if size_mb > 50:
        print(
            "warning: over Lambda's 50 MiB direct-upload limit -- this zip "
            "is only ever uploaded to S3 (releases/<tag>/migration-runner.zip) "
            "and referenced via Code.from_bucket, so that ceiling does not "
            "block a deploy, but flagging in case that assumption changes.",
            file=sys.stderr,
        )
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=Path("dist/migration-runner.zip"))
    p.add_argument("--build-dir", type=Path, default=Path("build/migration-runner"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    args.build_dir.mkdir(parents=True, exist_ok=True)
    try:
        build(args.out, build_dir=args.build_dir)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"package build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
