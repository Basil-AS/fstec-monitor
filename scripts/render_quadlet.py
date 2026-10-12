"""Render the reviewed Quadlet template with an immutable GHCR image reference."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PLACEHOLDER = "__FSTEC_MONITOR_IMAGE__"
IMAGE_REFERENCE = re.compile(
    r"ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[a-f0-9]{64}"
)
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "systemd/fstec-monitor.container.in"


def validate_image_reference(image_reference: str) -> str:
    if not IMAGE_REFERENCE.fullmatch(image_reference):
        raise ValueError("image must be a ghcr.io reference pinned by a 64-character sha256 digest")
    return image_reference


def render_template(template: str, image_reference: str) -> str:
    validate_image_reference(image_reference)
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"template must contain exactly one {PLACEHOLDER} placeholder")
    return template.replace(PLACEHOLDER, image_reference)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="immutable ghcr.io/...@sha256:<64 hex> reference")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output", type=Path, help="write the rendered unit here instead of stdout")
    args = parser.parse_args()

    try:
        rendered = render_template(args.template.read_text(encoding="utf-8"), args.image)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
