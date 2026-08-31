"""Synchronize the canonical M3 dashboard page into its Node-RED Flow node."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


NODE_ID = "m3_prod_page_template"
MARKER = "  <script>\n    (() => {"
AUTH_GLOBALS = (
    "  <script>window.__M3_DASHBOARD_AUTH_MODE__ = "
    "{{{m3DashboardAuthModeJson}}}; "
    "window.__M3_NOCOBASE_PARENT_ORIGIN__ = "
    "{{{m3NocobaseParentOriginJson}}};</script>\n"
)
ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "m3" / "node_red" / "m3_production_gateway_page.html"
FLOW = ROOT / "m3" / "node_red" / "m3_production_gateway_flow.json"


def expected_template(page_path: Path) -> str:
    page = page_path.read_text(encoding="utf-8")
    if page.count(MARKER) != 1:
        raise ValueError("canonical page must contain exactly one Flow injection marker")
    return page.replace(MARKER, AUTH_GLOBALS + MARKER)


def read_flow(flow_path: Path) -> list[dict[str, object]]:
    value = json.loads(flow_path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("production Flow must be a JSON array")
    return value


def template_node(flow: list[dict[str, object]]) -> dict[str, object]:
    matches = [node for node in flow if node.get("id") == NODE_ID]
    if len(matches) != 1:
        raise ValueError("production Flow must contain exactly one page template node")
    node = matches[0]
    if node.get("type") != "template" or not isinstance(node.get("template"), str):
        raise ValueError("production page node must be a template with string content")
    return node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the Flow template is stale")
    parser.add_argument("--page", type=Path, default=PAGE, help=argparse.SUPPRESS)
    parser.add_argument("--flow", type=Path, default=FLOW, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    flow = read_flow(args.flow)
    node = template_node(flow)
    expected = expected_template(args.page)
    if node["template"] == expected:
        return 0
    if args.check:
        print("m3 production Flow template is out of sync", file=sys.stderr)
        return 1

    node["template"] = expected
    args.flow.write_text(
        json.dumps(flow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
