import unittest

from m2.station_efficiency_history import HistoryError, normalize_rule


RULE = normalize_rule({
    "station_id": "ES02",
    "enabled": True,
    "inverter_min_running_power_kw": 5,
    "inverter_low_load_threshold_pct": 20,
    "inverter_trigger_minutes": 3,
    "inverter_recovery_minutes": 2,
    "temperature_rise_window_minutes": 5,
    "temperature_rise_threshold_c": 3,
    "temperature_trigger_minutes": 2,
    "temperature_recovery_minutes": 2,
    "chain_low_efficiency_threshold_pct": 85,
    "chain_low_efficiency_trigger_minutes": 2,
    "chain_low_efficiency_recovery_minutes": 2,
    "version": 1,
    "updated_at": "2026-08-27T09:00:00+08:00",
})


def timestamp(minute):
    return f"2026-08-27T10:{minute:02d}:00+08:00"


def minute_point(
    minute, *, pv_storage=90, storage_load=90, pv_load=90,
    pv_storage_input=None,
):
    point = {
        "station_id": "ES02",
        "data_time": timestamp(minute),
        "pv_storage_efficiency": pv_storage,
        "storage_load_efficiency": storage_load,
        "pv_load_efficiency": pv_load,
    }
    if pv_storage_input is not None:
        point["pv_storage_input_kw"] = pv_storage_input
    return point


def inverter_point(device_id, minute, power, *, name=None):
    return {
        "station_id": "ES02",
        "device_type": "pv_inverter",
        "device_id": device_id,
        "device_name": name or f"逆变器 {device_id}",
        "data_time": timestamp(minute),
        "source_time": timestamp(minute),
        "active_power_kw": power,
        "rated_power_kw": 60,
        "load_rate_pct": power / 60 * 100,
        "battery_power_kw": None,
        "temperature_c": None,
    }


def battery_point(device_id, minute, temperature, *, name=None):
    return {
        "station_id": "ES02",
        "device_type": "battery_cabinet",
        "device_id": device_id,
        "device_name": name or f"电池柜 {device_id}",
        "data_time": timestamp(minute),
        "source_time": timestamp(minute),
        "active_power_kw": None,
        "rated_power_kw": None,
        "load_rate_pct": None,
        "battery_power_kw": 10,
        "temperature_c": temperature,
    }


class StationEfficiencyBottleneckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from m2.station_efficiency_bottlenecks import evaluate_station_bottlenecks

        cls.evaluate = staticmethod(evaluate_station_bottlenecks)

    def evaluate_bottlenecks(self, *, minute_points, device_points, active_events=()):
        return self.evaluate(
            station_id="ES02",
            minute_points=minute_points,
            device_points=device_points,
            active_events=list(active_events),
            rule=RULE,
        )

    def test_opens_chain_and_inverter_events_independently(self):
        updates = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(0, storage_load=80),
                minute_point(1, storage_load=81),
            ],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 10),
                inverter_point("emu1", 2, 10),
            ],
        )

        self.assertEqual(
            {(event["event_type"], event["device_id"]) for event in updates},
            {("chain_low_efficiency", "storage_load"), ("inverter_low_load", "emu1")},
        )

    def test_chain_snapshots_include_only_relevant_confirmation_minute_devices(self):
        updates = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(0, pv_storage=80, storage_load=80, pv_load=80),
                minute_point(1, pv_storage=81, storage_load=81, pv_load=81),
            ],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 11),
                inverter_point("emu1", 2, 12),
                battery_point("cab-1", 0, None),
                battery_point("cab-1", 1, None),
                battery_point("cab-1", 2, 33),
            ],
        )
        snapshots = {
            event["device_id"]: event["evidence"]["trigger_device_snapshot"]
            for event in updates
            if event["event_type"] == "chain_low_efficiency"
        }

        self.assertEqual(set(snapshots["pv_load"]), {"pv_inverters"})
        self.assertEqual(
            set(snapshots["pv_storage"]), {"pv_inverters", "battery_cabinets"},
        )
        self.assertEqual(set(snapshots["storage_load"]), {"battery_cabinets"})
        self.assertEqual(
            snapshots["pv_load"]["pv_inverters"][0]["data_time"], timestamp(1),
        )
        self.assertIsNone(
            snapshots["storage_load"]["battery_cabinets"][0]["temperature_c"],
        )

    def test_omits_unchanged_events_and_returns_a_recovery_once(self):
        opening_points = [minute_point(0, storage_load=80), minute_point(1, storage_load=81)]
        active = self.evaluate_bottlenecks(
            minute_points=opening_points,
            device_points=[],
        )[0]

        self.assertEqual(
            self.evaluate_bottlenecks(
                minute_points=opening_points,
                device_points=[],
                active_events=[active],
            ),
            [],
        )

        recovery = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(1, storage_load=81),
                minute_point(2, storage_load=85),
                minute_point(3, storage_load=86),
            ],
            device_points=[],
            active_events=[active],
        )
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["status"], "recovered")
        self.assertEqual(recovery[0]["end_time"], timestamp(2))
        self.assertEqual(
            self.evaluate_bottlenecks(
                minute_points=[minute_point(2, storage_load=85), minute_point(3, storage_load=86)],
                device_points=[],
            ),
            [],
        )

    def test_chain_event_recovers_when_station_minutes_show_stopped_input(self):
        active = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(0, pv_storage=80),
                minute_point(1, pv_storage=81),
            ],
            device_points=[],
        )[0]

        recovery = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(1, pv_storage=81),
                minute_point(2, pv_storage=None, pv_storage_input=0),
                minute_point(3, pv_storage=None, pv_storage_input=0),
            ],
            device_points=[],
            active_events=[active],
        )

        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0]["status"], "recovered")
        self.assertEqual(recovery[0]["end_time"], timestamp(2))
        self.assertEqual(recovery[0]["evidence"]["recovery_reason"], "chain_stopped")

    def test_active_pending_chain_becomes_diagnosed_without_replacing_snapshot(self):
        active_chain = self.evaluate_bottlenecks(
            minute_points=[minute_point(0, pv_storage=80), minute_point(1, pv_storage=81)],
            device_points=[battery_point("cab-1", 1, None)],
        )[0]
        original_snapshot = active_chain["evidence"]["trigger_device_snapshot"]

        updates = self.evaluate_bottlenecks(
            minute_points=[minute_point(2, pv_storage=None)],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 10),
                inverter_point("emu1", 2, 10),
            ],
            active_events=[active_chain],
        )
        chain = next(event for event in updates if event["event_type"] == "chain_low_efficiency")

        self.assertEqual(chain["evidence"]["cause_status"], "diagnosed")
        self.assertEqual(chain["evidence"]["diagnosed_causes"], [{
            "event_type": "inverter_low_load",
            "device_id": "emu1",
            "device_name": "逆变器 emu1",
        }])
        self.assertEqual(chain["evidence"]["trigger_device_snapshot"], original_snapshot)

    def test_active_diagnosed_chain_returns_to_pending_when_cause_recovers(self):
        active_chain = self.evaluate_bottlenecks(
            minute_points=[minute_point(0, pv_storage=80), minute_point(1, pv_storage=81)],
            device_points=[battery_point("cab-1", 1, None)],
        )[0]
        diagnosis_updates = self.evaluate_bottlenecks(
            minute_points=[minute_point(2, pv_storage=None)],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 10),
                inverter_point("emu1", 2, 10),
            ],
            active_events=[active_chain],
        )
        active_inverter = next(
            event for event in diagnosis_updates if event["event_type"] == "inverter_low_load"
        )
        diagnosed_chain = {
            **active_chain,
            "evidence": {
                **active_chain["evidence"],
                "cause_status": "diagnosed",
                "diagnosed_causes": [{
                    "event_type": "inverter_low_load",
                    "device_id": "emu1",
                    "device_name": "逆变器 emu1",
                }],
            },
        }
        original_snapshot = diagnosed_chain["evidence"]["trigger_device_snapshot"]

        updates = self.evaluate_bottlenecks(
            minute_points=[minute_point(3, pv_storage=None)],
            device_points=[inverter_point("emu1", 3, 0), inverter_point("emu1", 4, 0)],
            active_events=[diagnosed_chain, active_inverter],
        )
        chain = next(event for event in updates if event["event_type"] == "chain_low_efficiency")

        self.assertEqual(chain["status"], "active")
        self.assertEqual(chain["evidence"]["cause_status"], "pending")
        self.assertEqual(chain["evidence"]["diagnosed_causes"], [])
        self.assertEqual(chain["evidence"]["trigger_device_snapshot"], original_snapshot)

    def test_rejects_duplicate_active_event_identities(self):
        active = self.evaluate_bottlenecks(
            minute_points=[minute_point(0, storage_load=80), minute_point(1, storage_load=81)],
            device_points=[],
        )[0]

        with self.assertRaises(HistoryError) as caught:
            self.evaluate_bottlenecks(
                minute_points=[minute_point(2, storage_load=82)],
                device_points=[],
                active_events=[active, dict(active)],
            )
        self.assertEqual(caught.exception.code, "duplicate_active_event")

    def test_rejects_an_active_chain_event_without_a_known_chain_identity(self):
        active = self.evaluate_bottlenecks(
            minute_points=[minute_point(0, storage_load=80), minute_point(1, storage_load=81)],
            device_points=[],
        )[0]

        with self.assertRaises(HistoryError) as caught:
            self.evaluate_bottlenecks(
                minute_points=[minute_point(2, storage_load=82)],
                device_points=[],
                active_events=[dict(active, device_id="not-a-chain")],
            )
        self.assertEqual(caught.exception.code, "invalid_active_event")

    def test_uses_shanghai_times_for_device_events_and_snapshots(self):
        utc = lambda minute: f"2026-08-27T02:{minute:02d}:00Z"
        points = [
            dict(inverter_point("emu1", minute, 10), data_time=utc(minute), source_time=utc(minute))
            for minute in range(3)
        ]
        points[1]["source_time"] = "2026-08-27T02:01:30Z"
        updates = self.evaluate_bottlenecks(
            minute_points=[
                dict(minute_point(0, pv_load=80), data_time=utc(0)),
                dict(minute_point(1, pv_load=81), data_time=utc(1)),
            ],
            device_points=points,
        )
        inverter = next(event for event in updates if event["event_type"] == "inverter_low_load")
        chain = next(event for event in updates if event["device_id"] == "pv_load")

        self.assertEqual(inverter["last_seen_time"], "2026-08-27T10:02:00+08:00")
        snapshot = chain["evidence"]["trigger_device_snapshot"]["pv_inverters"][0]
        self.assertEqual(snapshot["data_time"], "2026-08-27T10:01:00+08:00")
        self.assertEqual(snapshot["source_time"], "2026-08-27T10:01:30+08:00")

    def test_delayed_open_uses_rule_confirmation_minute_not_latest_history(self):
        updates = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(2, pv_load=82),
                minute_point(0, pv_load=80),
                minute_point(1, pv_load=81),
            ],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 11),
                inverter_point("emu1", 2, 12),
            ],
        )
        chain = next(event for event in updates if event["device_id"] == "pv_load")

        self.assertEqual(
            chain["evidence"]["trigger_device_snapshot"]["pv_inverters"][0]["data_time"],
            timestamp(1),
        )
        self.assertEqual(chain["evidence"]["confirmation_time"], timestamp(1))
        self.assertEqual(chain["last_seen_time"], timestamp(2))
        self.assertEqual(chain["evidence"]["diagnosed_causes"], [])

    def test_device_cause_confirmed_after_chain_confirmation_is_not_initial_diagnosis(self):
        updates = self.evaluate_bottlenecks(
            minute_points=[
                minute_point(0, pv_load=80),
                minute_point(1, pv_load=81),
                minute_point(2, pv_load=82),
            ],
            device_points=[
                inverter_point("emu1", 0, 10),
                inverter_point("emu1", 1, 10),
                inverter_point("emu1", 2, 10),
            ],
        )
        chain = next(event for event in updates if event["device_id"] == "pv_load")
        inverter = next(
            event for event in updates if event["event_type"] == "inverter_low_load"
        )

        self.assertEqual(inverter["evidence"]["confirmation_time"], timestamp(2))
        self.assertEqual(chain["evidence"]["confirmation_time"], timestamp(1))
        self.assertEqual(chain["evidence"]["cause_status"], "pending")
        self.assertEqual(chain["evidence"]["diagnosed_causes"], [])


if __name__ == "__main__":
    unittest.main()
