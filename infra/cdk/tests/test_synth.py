from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk.assertions import Template


def synth_template() -> Template:
    app = cdk.App()
    from fde_cdk.stack import FdePlatformStack  # local import: keep collection cheap

    stack = FdePlatformStack(app, "FdePlatform")
    return Template.from_stack(stack)


def test_stack_synthesizes() -> None:
    template = synth_template()
    assert template.to_json()["Description"].startswith("FDE Agent Platform")


def test_no_cdk_assets_anywhere() -> None:
    """The launch-button contract: a fresh account with no CDK bootstrap.
    Any cdk-managed asset parameter or bootstrap-version rule breaks it."""
    raw = json.dumps(synth_template().to_json())
    assert "cdk-hnb659fds-assets" not in raw  # default bootstrap qualifier
    assert "BootstrapVersion" not in synth_template().to_json().get("Parameters", {})
