import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("render_quadlet", ROOT / "scripts/render_quadlet.py")
assert _SPEC is not None and _SPEC.loader is not None
_RENDERER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RENDERER)
render_template = _RENDERER.render_template
validate_image_reference = _RENDERER.validate_image_reference


def test_renderer_requires_immutable_ghcr_digest_and_replaces_placeholder():
    image = "ghcr.io/teya/fstec-monitor@sha256:" + "a" * 64
    template = "Image=__FSTEC_MONITOR_IMAGE__\n"

    assert render_template(template, image) == f"Image={image}\n"
    assert validate_image_reference(image) == image


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/teya/fstec-monitor:latest",
        "docker.io/teya/fstec-monitor@sha256:" + "a" * 64,
        "ghcr.io/teya/fstec-monitor@sha256:" + "a" * 63,
        "ghcr.io/teya/fstec-monitor@sha256:" + "g" * 64,
        "ghcr.io/teya/fstec-monitor@sha256:" + "a" * 64 + "#fragment",
    ],
)
def test_renderer_rejects_mutable_or_malformed_image_references(image):
    with pytest.raises(ValueError):
        validate_image_reference(image)


def test_renderer_requires_one_template_placeholder():
    image = "ghcr.io/teya/fstec-monitor@sha256:" + "b" * 64
    with pytest.raises(ValueError, match="placeholder"):
        render_template("Image=ghcr.io/teya/fstec-monitor:latest\n", image)
    with pytest.raises(ValueError, match="placeholder"):
        render_template("__FSTEC_MONITOR_IMAGE__ __FSTEC_MONITOR_IMAGE__", image)


def test_quadlet_template_enforces_runtime_contract():
    template = (ROOT / "systemd/fstec-monitor.container.in").read_text()
    assert "Image=__FSTEC_MONITOR_IMAGE__" in template
    assert "Network=bots-net" in template
    assert "Environment=FSTEC_TELEGRAM_API_ROOT=http://telegram-bot-api:8081" in template
    assert "EnvironmentFile=/srv/runtime/secrets/fstec-monitor.env" in template
    assert "EnvironmentFile=-/srv/runtime/secrets/telegram-admin.env" in template
    assert "Volume=/srv/runtime/apps/fstec-monitor/data:/data:Z" in template
    assert ":U" not in template
    assert "PublishPort=" not in template
    assert "User=1002:1002" in template
    assert "ReadOnly=true" in template
    assert "DropCapability=all" in template
    assert "NoNewPrivileges=true" in template
    assert "Tmpfs=/tmp" in template
    assert "LogDriver=journald" in template
    assert "Type=notify" in template
    assert "WatchdogSec=" in template
    assert "Restart=on-failure" in template
    assert "TasksMax=" in template
    assert "CPUWeight=" in template
    assert "MemoryHigh=" in template
    assert "MemoryMax=" in template
    assert "telegram-bot-api.service" in template
    assert "Timer" not in template


def test_containerfile_is_pinned_and_nonroot_with_quadlet_healthcheck():
    containerfile = (ROOT / "Containerfile").read_text()
    quadlet = (ROOT / "systemd/fstec-monitor.container.in").read_text()
    assert "python:3.14-slim-trixie" in containerfile
    assert "ghcr.io/astral-sh/uv:0.12.19" in containerfile
    assert "USER 1002:1002" in containerfile
    assert "HEALTHCHECK" not in containerfile
    assert "fstec-monitor-bot" in containerfile
    assert "HealthCmd=/app/.venv/bin/python -m fstec_monitor.healthcheck" in quadlet


def test_containerignore_excludes_secrets_state_and_build_noise():
    ignored = (ROOT / ".containerignore").read_text()
    for pattern in (".git", ".venv", ".env", "data", "dist", "build", "__pycache__"):
        assert pattern in ignored


def test_readme_describes_pending_digest_single_scheduler_and_snapshot_policy():
    readme = (ROOT / "README.md").read_text()
    assert "FSTEC_MONITOR_IMAGE" in readme
    assert "ghcr.io/<owner>/<image>@sha256:<64 hex>" in readme
    assert "digest" in readme.lower()
    assert "ровно один scheduler" in readme.lower()
    assert "markup-only" in readme.lower()
    assert "snapshots" in readme.lower()
