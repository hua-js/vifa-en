"""Read-only history API contract and calendar boundaries."""
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from m2.tests.test_energy_efficiency_api import energy_api, ENVIRONMENT
from m2.station_efficiency_history import build_minute_point

NOW = datetime.fromisoformat("2026-09-07T02:30:00Z")


def point(timestamp, station="ES02", input_kw=100, output_kw=90):
    return build_minute_point(station, timestamp, {
        "pv_storage": {"efficiency": output_kw / input_kw * 100, "input_kw": input_kw, "output_kw": output_kw},
        "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
        "pv_load": {"efficiency": None, "input_kw": None, "output_kw": None},
    }, "v1", timestamp)


def event(start, end=None, station="ES02", id=1):
    return {
        "id": id, "station_id": station, "event_type": "inverter_low_load",
        "device_name": "1#逆变器", "start_time": start, "end_time": end,
        "status": "recovered" if end else "active",
        "evidence": {"display_text": "负载率最低 12%"}, "impact_chain": ["光→用"],
    }


class HistoryQueryTests(unittest.TestCase):
    def query(self, args, points=(), events=()):
        source = Mock(side_effect=AssertionError("history must not fetch live source"))
        calculate = Mock(side_effect=AssertionError("history must not calculate live efficiency"))
        minute = Mock(side_effect=AssertionError("history must not write"))
        fetch_points = Mock(return_value=list(points))
        fetch_events = Mock(return_value=list(events))
        result = energy_api.execute(args, ENVIRONMENT, now=NOW,
            fetch_station=source, calculate=calculate, process_minute=minute,
            fetch_points=fetch_points, fetch_events=fetch_events)
        source.assert_not_called()
        calculate.assert_not_called()
        minute.assert_not_called()
        return result, fetch_points, fetch_events

    def test_history_reads_full_day_keeps_gaps_and_uses_weighted_efficiency(self):
        (payload, code), points, events = self.query(
            ["history", "ES02", "2026-09-06"],
            points=[
                point("2026-09-05T23:59:00+08:00"),
                point("2026-09-06T00:00:00+08:00", input_kw=100, output_kw=80),
                point("2026-09-06T23:59:00+08:00", input_kw=300, output_kw=270),
                point("2026-09-07T00:00:00+08:00"),
                point("2026-09-06T08:00:00+08:00", station="ES01"),
            ],
            events=[event("2026-09-05T23:00:00+08:00", "2026-09-07T01:00:00+08:00")],
        )
        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertEqual(data["operation"], "history")
        self.assertEqual(data["station_id"], "ES02")
        self.assertNotIn("realtime", data)
        self.assertEqual(len(data["trend"]), 2)
        self.assertIsNone(data["trend"][0]["storageLoad"])
        self.assertAlmostEqual(data["summary"]["pv_storage_efficiency"], 87.5)
        self.assertEqual(data["events"][0]["status"], "已恢复")
        self.assertEqual(data["range"]["cutoff_time"], "2026-09-07T00:00:00+08:00")
        for fetch in (points, events):
            self.assertEqual(fetch.call_args.args[:3], ("ES02", "2026-09-06T00:00:00+08:00", "2026-09-07T00:00:00+08:00"))

    def test_today_history_uses_beijing_date_and_omits_future_minutes(self):
        (payload, code), _, _ = self.query(["history", "ES02", "2026-09-07"], points=[
            point("2026-09-07T10:30:00+08:00"), point("2026-09-07T10:31:00+08:00"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["data"]["trend"]), 1)
        self.assertEqual(payload["data"]["range"]["end_time"], "2026-09-08T00:00:00+08:00")

    def test_events_include_overlap_not_only_events_recovered_within_range(self):
        (payload, code), points, events = self.query(["events", "ES02", "2026-09-01", "2026-09-06"], events=[
            event("2026-08-31T23:00:00+08:00", "2026-09-07T01:00:00+08:00", id=1),
            event("2026-09-06T12:00:00+08:00", id=2),
            event("2026-09-07T00:00:00+08:00", id=3),
            event("2026-08-30T00:00:00+08:00", "2026-08-31T23:59:00+08:00", id=4),
            event("2026-09-02T12:00:00+08:00", station="ES01", id=5),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["operation"], "events")
        self.assertEqual([row["id"] for row in payload["data"]["events"]], [1, 2])
        points.assert_not_called()
        self.assertEqual(events.call_args.args[1:3], ("2026-09-01T00:00:00+08:00", "2026-09-07T00:00:00+08:00"))

    def test_empty_history_is_success_without_fabricated_points(self):
        (payload, code), _, _ = self.query(["history", "ES01", "2026-01-01"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["trend"], [])
        self.assertIsNone(payload["data"]["summary"]["pv_storage_efficiency"])
        self.assertIsNone(payload["data"]["range"]["latest_time"])

    def test_history_does_not_require_live_source_credentials(self):
        with patch.object(energy_api, "LOCAL_EMU_URL", ""), patch.object(energy_api, "LOCAL_EMU_TOKEN", ""):
            payload, code = energy_api.execute(["history", "ES01", "2026-09-06"], {
                key: value for key, value in ENVIRONMENT.items() if not key.startswith("VIFA_EMU_")
            }, now=NOW, fetch_points=lambda *args: [], fetch_events=lambda *args: [])
        self.assertEqual((payload["status"], code), ("ok", 0))

    def test_invalid_queries_are_rejected_before_reading_records(self):
        invalid = [
            ["history", "ES02", "2026-02-30"], ["history", "ES02", "20260906"],
            ["history", "ES02", "2026-09-06;id"], ["history", "ES03", "2026-09-06"],
            ["history", "ES02", "2026-09-08"], ["history", "ES02"],
            ["events", "ES02", "2026-09-07", "2026-09-06"],
            ["events", "ES02", "2026-08-01", "2026-09-01"],
            ["events", "ES02", "2026-09-01", "2026-09-08"],
        ]
        for args in invalid:
            with self.subTest(args=args):
                (payload, code), points, events = self.query(args)
                self.assertEqual(code, 2)
                self.assertEqual(payload["error"]["code"], "invalid_arguments")
                points.assert_not_called()
                events.assert_not_called()

    def test_thirty_one_days_inclusive_is_accepted(self):
        (payload, code), _, events = self.query(["events", "ES02", "2026-08-08", "2026-09-07"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["events"], [])
        events.assert_called_once()


if __name__ == "__main__":
    unittest.main()
