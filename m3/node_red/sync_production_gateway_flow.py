"""Synchronize the canonical M3 dashboard page into its Node-RED Flow node."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


NODE_ID = "m3_prod_page_template"
CUSTOM_PREPARE_NODE_ID = "m3_prod_custom_prepare"
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
TEMPLATE_HTML = ROOT / "m3" / "node_red" / "m3_production_gateway_template.html"
LATEST_ROUTE_ANCHOR = (
    "} else if (route === '/energy-forecast-api/custom-runs/:run_id' "
    "&& method === 'GET') {"
)
LATEST_ROUTE_BRANCH = """} else if (route === '/energy-forecast-api/custom-runs/:station_key/latest' && method === 'GET') {
  if (!exactKeys(query, ['interval_seconds', 'forecast_days'])) return response(400, 'invalid_request', '请求不正确');
  const intervalSeconds = Number(query.interval_seconds);
  const forecastDays = Number(query.forecast_days);
  if (!Number.isInteger(intervalSeconds) || ![30, 60, 300, 900, 1800, 3600].includes(intervalSeconds) || !Number.isInteger(forecastDays) || forecastDays < 1 || forecastDays > 7) return response(422, 'invalid_request', '查询参数无效');
  const stationId = stations[params.station_key];
  if (!stationId) return response(404, 'not_found', '电站不存在');
  target = `http://localhost/v1/stations/${stationId}/custom-forecast-runs/latest?interval_seconds=${intervalSeconds}&forecast_days=${forecastDays}`;
"""


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


def custom_prepare_node(flow: list[dict[str, object]]) -> dict[str, object]:
    matches = [node for node in flow if node.get("id") == CUSTOM_PREPARE_NODE_ID]
    if len(matches) != 1:
        raise ValueError("production Flow must contain exactly one custom request validator")
    node = matches[0]
    if node.get("type") != "function" or not isinstance(node.get("func"), str):
        raise ValueError("custom request validator must be a function node")
    return node


def expected_custom_prepare(source: str) -> str:
    route_marker = "/energy-forecast-api/custom-runs/:station_key/latest"
    if source.count(LATEST_ROUTE_BRANCH) == 1:
        return source
    if route_marker in source:
        raise ValueError("custom request validator latest route has drifted")
    if source.count(LATEST_ROUTE_ANCHOR) != 1:
        raise ValueError("custom request validator latest-route anchor is invalid")
    return source.replace(
        LATEST_ROUTE_ANCHOR,
        LATEST_ROUTE_BRANCH + LATEST_ROUTE_ANCHOR,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the Flow template is stale")
    parser.add_argument("--page", type=Path, default=PAGE, help=argparse.SUPPRESS)
    parser.add_argument("--flow", type=Path, default=FLOW, help=argparse.SUPPRESS)
    parser.add_argument(
        "--template-html", type=Path, default=TEMPLATE_HTML, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    flow = read_flow(args.flow)
    node = template_node(flow)
    custom_node = custom_prepare_node(flow)
    expected = expected_template(args.page)
    expected_custom = expected_custom_prepare(custom_node["func"])
    template_html_current = (
        args.template_html.exists()
        and args.template_html.read_text(encoding="utf-8") == expected
    )
    if (
        node["template"] == expected
        and custom_node["func"] == expected_custom
        and template_html_current
    ):
        return 0
    if args.check:
        print("m3 production page artifacts are out of sync", file=sys.stderr)
        return 1

    node["template"] = expected
    custom_node["func"] = expected_custom
    args.flow.write_text(
        json.dumps(flow, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.template_html.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
