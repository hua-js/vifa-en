"""Authoritative current load score shared by API responses and saved evaluations."""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

LOAD_SCORE_POLICY = "load-night-weighted-mape-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def current_load_score(points, *, run_id, calculated_at):
    weighted_error = total_weight = 0.0
    actual_count = valid_count = 0
    for point in points:
        actual, forecast = point.get("actual_value"), point.get("forecast_value")
        if (point.get("actual_quality") != "valid"
                or type(actual) not in (int, float) or not math.isfinite(actual)
                or type(forecast) not in (int, float) or not math.isfinite(forecast)):
            continue
        actual_count += 1
        if actual == 0:
            continue
        at = point["target_time"]
        if isinstance(at, str):
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if at.utcoffset() is None:
            raise ValueError("Score timestamp must be timezone aware")
        hour = at.astimezone(SHANGHAI).hour
        night = hour < 7 or hour >= 21
        weight = 0.25 if night else 1.0
        error = abs(forecast - actual)
        total_weight += weight
        valid_count += 1
        if not (night and error < 10):
            weighted_error += weight * error / abs(actual) * 100
    return {
        "policy": LOAD_SCORE_POLICY,
        "run_id": run_id,
        "unique_id": "station_total_load",
        "mape_percent": weighted_error / total_weight if total_weight else None,
        "actual_count": actual_count,
        "valid_count": valid_count,
        "calculated_at": calculated_at.isoformat(),
    }
