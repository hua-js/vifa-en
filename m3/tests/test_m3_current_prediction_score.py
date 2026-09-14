"""Boundary coverage for the score returned by M3 and persisted with evaluations."""
import unittest
from datetime import datetime, timezone
from m3.worker.domain.current_prediction_score import current_load_score

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def point(time, actual, forecast, quality="valid"):
    return dict(target_time=time, actual_value=actual, forecast_value=forecast, actual_quality=quality)


def score(points):
    return current_load_score(points, run_id="test-run", calculated_at=NOW)


class ScoreTests(unittest.TestCase):
    def test_night_tolerance_retains_denominator(self):
        result = score([
            point("2026-09-12T01:00:00+08:00", 1, 9),
            point("2026-09-12T21:00:00+08:00", 1, 9),
            point("2026-09-12T12:00:00+08:00", 100, 105),
        ])
        self.assertAlmostEqual(result["mape_percent"], 5 / 1.5)
        self.assertEqual(result["valid_count"], 3)

    def test_time_and_tolerance_boundaries(self):
        for time, expected in [("06:59:59", 0), ("07:00:00", 5), ("20:59:59", 5), ("21:00:00", 0)]:
            self.assertEqual(score([point(f"2026-09-12T{time}+08:00", 100, 105)])["mape_percent"], expected)
        self.assertEqual(score([point("2026-09-12T17:00:00Z", 100, 110)])["mape_percent"], 10)
        self.assertEqual(score([point("2026-09-12T01:00:00+08:00", 100, 90)])["mape_percent"], 10)

    def test_invalid_and_zero_points(self):
        result = score([point("2026-09-12T01:00:00+08:00", actual, forecast, quality)
                        for actual, forecast, quality in [(0, 10, "valid"), (None, 10, "valid"),
                        (100, float("nan"), "valid"), (100, 110, "invalid")]])
        self.assertIsNone(result["mape_percent"])
        self.assertEqual(result["actual_count"], 1)
        self.assertEqual(result["valid_count"], 0)
