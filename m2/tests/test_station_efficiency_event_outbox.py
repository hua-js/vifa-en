import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from m2.station_efficiency_event_outbox import (
    EventOutboxError,
    enqueue_event,
    flush_station_events,
    load_station_events,
    pending_event_count,
)


def event(*, station_id="ES01", status="active", end_time=None, observed=80):
    return {
        "station_id": station_id,
        "event_type": "chain_low_efficiency",
        "device_id": "storage_load",
        "device_name": "储→用",
        "start_time": "2026-08-27T10:00:00+08:00",
        "end_time": end_time,
        "last_seen_time": "2026-08-27T10:02:00+08:00",
        "status": status,
        "observed_value": observed,
        "threshold_value": 85,
        "observed_unit": "%",
        "evidence": {"display_text": "固定公开摘要"},
        "impact_chain": ["储→用"],
        "rule_version": 1,
    }


class StationEfficiencyEventOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = str(Path(self.temporary_directory.name) / "events.sqlite3")

    def test_same_event_identity_overwrites_active_with_recovered_payload(self):
        enqueue_event(event(), self.path)
        recovered = event(
            status="recovered",
            end_time="2026-08-27T10:01:00+08:00",
            observed=79,
        )
        enqueue_event(recovered, self.path)

        queued = load_station_events("ES01", self.path)

        self.assertEqual(queued, [recovered])
        self.assertEqual(queued[0]["start_time"], "2026-08-27T10:00:00+08:00")
        self.assertEqual(queued[0]["end_time"], "2026-08-27T10:01:00+08:00")

    def test_successful_flush_deletes_event(self):
        queued_event = event()
        enqueue_event(queued_event, self.path)
        saved = []

        result = flush_station_events(
            "ES01",
            self.path,
            save_event=lambda payload: saved.append(payload),
        )

        self.assertEqual(saved, [queued_event])
        self.assertEqual(result, {
            "attempted": 1, "saved": 1, "failed": 0, "outbox_pending": 0,
        })
        self.assertEqual(pending_event_count("ES01", self.path), 0)

    def test_failed_flush_retains_event_without_exception_details(self):
        queued_event = event()
        enqueue_event(queued_event, self.path)

        result = flush_station_events(
            "ES01",
            self.path,
            save_event=lambda payload: (_ for _ in ()).throw(
                RuntimeError("transport-secret")
            ),
        )

        self.assertEqual(result, {
            "attempted": 1, "saved": 0, "failed": 1, "outbox_pending": 1,
        })
        self.assertEqual(load_station_events("ES01", self.path), [queued_event])
        self.assertNotIn("transport-secret", repr(result))

    def test_two_stations_can_enqueue_concurrently_without_losing_rows(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(
                lambda station_id: enqueue_event(
                    event(station_id=station_id), self.path,
                ),
                ("ES01", "ES02"),
            ))

        self.assertEqual(pending_event_count("ES01", self.path), 1)
        self.assertEqual(pending_event_count("ES02", self.path), 1)

    def test_sqlite_errors_use_fixed_sanitized_message(self):
        secret_path = str(Path(self.temporary_directory.name) / "private-value" / "events.sqlite3")
        with self.assertRaises(EventOutboxError) as caught:
            enqueue_event(event(), secret_path)

        self.assertEqual(str(caught.exception), "本机事件补偿队列不可用")
        self.assertNotIn("private-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
