import unittest

from m2.station_efficiency_history import (
    HistoryError,
    build_calendar_day_dashboard,
    build_event_upsert,
    build_minute_point,
    build_minute_upsert,
    evaluate_battery_temperature_rise,
    evaluate_chain_low_efficiency,
    evaluate_inverter_low_load,
    normalize_rule,
)


class StationEfficiencyHistoryTests(unittest.TestCase):
    def inverter_rule(self):
        return normalize_rule({
            "station_id": "station-1",
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
            "version": 4,
            "updated_at": "2026-08-25T00:00:00+08:00",
        })

    def inverter_sample(self, minute, power):
        return {
            "device_id": "inv-1",
            "device_name": "1#逆变器",
            "data_time": f"2026-08-25T10:{minute:02d}:00+08:00",
            "active_power_kw": power,
            "rated_power_kw": 100,
        }

    def temperature_sample(self, minute, temperature):
        return {
            "device_id": "battery-1",
            "device_name": "1#电池簇",
            "data_time": f"2026-08-25T11:{minute:02d}:00+08:00",
            "temperature_c": temperature,
        }

    def chain_sample(self, minute, efficiency, chain="storage_load"):
        return {
            "device_id": chain,
            "device_name": {
                "storage_load": "储→用",
                "pv_storage": "光→储",
                "pv_load": "光→用",
            }[chain],
            "data_time": f"2026-08-27T10:{minute:02d}:00+08:00",
            "efficiency_pct": efficiency,
        }

    def test_rule_requires_every_configured_value(self):
        for field in (
            "inverter_trigger_minutes",
            "chain_low_efficiency_threshold_pct",
            "chain_low_efficiency_trigger_minutes",
            "chain_low_efficiency_recovery_minutes",
        ):
            with self.subTest(field=field):
                record = dict(self.inverter_rule())
                del record[field]
                with self.assertRaises(HistoryError) as caught:
                    normalize_rule(record)
                self.assertEqual(caught.exception.code, "invalid_rule")

    def test_chain_rule_rejects_out_of_range_threshold_and_non_positive_minutes(self):
        for field, value in (
            ("chain_low_efficiency_threshold_pct", -0.01),
            ("chain_low_efficiency_threshold_pct", 100.01),
            ("chain_low_efficiency_threshold_pct", float("nan")),
            ("chain_low_efficiency_trigger_minutes", 0),
            ("chain_low_efficiency_recovery_minutes", 0),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(HistoryError) as caught:
                    normalize_rule(dict(self.inverter_rule(), **{field: value}))
                self.assertEqual(caught.exception.code, "invalid_rule")

    def test_rule_rejects_none_station_id(self):
        record = dict(self.inverter_rule(), station_id=None)
        with self.assertRaises(HistoryError) as caught:
            normalize_rule(record)
        self.assertEqual(caught.exception.code, "invalid_rule")

    def test_inverter_event_opens_at_first_qualifying_minute(self):
        samples = [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)]
        event = evaluate_inverter_low_load(samples, self.inverter_rule())
        self.assertEqual(event["start_time"], "2026-08-25T10:00:00+08:00")
        self.assertEqual(event["status"], "active")
        self.assertEqual(event["observed_value"], 10.0)
        self.assertEqual(event["impact_chain"], ["光→储", "光→用"])
        self.assertEqual(build_event_upsert(event)["key"], {
            "station_id": "station-1",
            "event_type": "inverter_low_load",
            "device_id": "inv-1",
            "start_time": "2026-08-25T10:00:00+08:00",
        })

    def test_inverter_event_exposes_structured_load_evidence(self):
        event = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        self.assertEqual(event["evidence"]["current_load_rate_pct"], 12.0)
        self.assertEqual(event["evidence"]["minimum_load_rate_pct"], 10.0)
        self.assertEqual(event["evidence"]["rated_power_kw"], 100.0)
        self.assertEqual(event["evidence"]["low_load_threshold_pct"], 20.0)
        self.assertEqual(event["evidence"]["continuous_minutes"], 3)
        self.assertEqual(
            event["evidence"]["confirmation_time"],
            "2026-08-25T10:02:00+08:00",
        )
        self.assertEqual(event["evidence"]["rule"]["version"], 4)

    def test_legacy_inverter_snapshot_without_chain_fields_stays_active_and_recovers(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        legacy_rule = {
            key: value for key, value in active["evidence"]["rule"].items()
            if not key.startswith("chain_low_efficiency_")
            and not key.startswith("temperature_")
        }
        legacy = {**active, "evidence": {**active["evidence"], "rule": legacy_rule}}
        changed_current_rule = dict(
            self.inverter_rule(),
            inverter_low_load_threshold_pct=5,
            inverter_recovery_minutes=9,
            version=5,
        )

        continued = evaluate_inverter_low_load(
            [self.inverter_sample(3, 10)], changed_current_rule, legacy,
        )
        self.assertEqual(continued["status"], "active")
        self.assertEqual(continued["evidence"]["continuous_minutes"], 4)
        self.assertEqual(continued["evidence"]["rule"], legacy_rule)

        recovered = evaluate_inverter_low_load(
            [self.inverter_sample(4, 0), self.inverter_sample(5, 0)],
            changed_current_rule,
            continued,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T10:04:00+08:00")

    def test_active_inverter_event_updates_low_load_evidence_without_counting_recovery(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        updated = evaluate_inverter_low_load(
            [self.inverter_sample(3, 11), self.inverter_sample(4, 10)],
            self.inverter_rule(), active,
        )
        self.assertEqual(updated["evidence"]["continuous_minutes"], 5)
        self.assertEqual(updated["evidence"]["current_load_rate_pct"], 10.0)
        self.assertEqual(updated["evidence"]["minimum_load_rate_pct"], 10.0)

        pending_recovery = evaluate_inverter_low_load(
            [self.inverter_sample(5, 0)], self.inverter_rule(), updated,
        )
        self.assertEqual(pending_recovery["status"], "active")
        self.assertEqual(pending_recovery["evidence"]["continuous_minutes"], 5)

    def test_active_inverter_event_deduplicates_overlapping_low_load_windows(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        expanded = evaluate_inverter_low_load(
            [self.inverter_sample(1, 10), self.inverter_sample(2, 12),
             self.inverter_sample(3, 11), self.inverter_sample(4, 10)],
            self.inverter_rule(), active,
        )
        self.assertEqual(expanded["evidence"]["continuous_minutes"], 5)

        repeated = evaluate_inverter_low_load(
            [self.inverter_sample(2, 12), self.inverter_sample(3, 11), self.inverter_sample(4, 10)],
            self.inverter_rule(), expanded,
        )
        self.assertEqual(repeated["evidence"]["continuous_minutes"], 5)

    def test_active_inverter_event_ignores_replayed_samples_before_last_seen(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        replayed = evaluate_inverter_low_load(
            [self.inverter_sample(minute, 0) for minute in range(12)],
            self.inverter_rule(), active,
        )
        self.assertEqual(replayed["status"], "active")
        self.assertIsNone(replayed["end_time"])
        self.assertEqual(replayed["last_seen_time"], "2026-08-25T10:12:00+08:00")
        self.assertEqual(replayed["observed_value"], 10.0)

    def test_active_inverter_event_uses_only_new_event_period_samples(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        advanced = evaluate_inverter_low_load(
            [self.inverter_sample(0, 5), self.inverter_sample(11, 5),
             self.inverter_sample(13, 14), self.inverter_sample(14, 13)],
            self.inverter_rule(), active,
        )
        self.assertEqual(advanced["status"], "active")
        self.assertEqual(advanced["last_seen_time"], "2026-08-25T10:14:00+08:00")
        self.assertEqual(advanced["observed_value"], 10.0)
        self.assertEqual(advanced["evidence"]["continuous_minutes"], 5)

    def test_active_inverter_event_requires_new_recovery_samples_after_start(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        replayed_recovery = evaluate_inverter_low_load(
            [self.inverter_sample(8, 0), self.inverter_sample(9, 0)],
            self.inverter_rule(), active,
        )
        self.assertEqual(replayed_recovery["status"], "active")
        self.assertIsNone(replayed_recovery["end_time"])

        recovered = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0), self.inverter_sample(14, 0)],
            self.inverter_rule(), replayed_recovery,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T10:13:00+08:00")
        self.assertGreaterEqual(recovered["end_time"], recovered["start_time"])

    def test_active_inverter_event_recovers_from_a_cross_call_minute_window(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        pending = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0)], self.inverter_rule(), active,
        )
        self.assertEqual(pending["status"], "active")
        self.assertEqual(pending["last_seen_time"], "2026-08-25T10:13:00+08:00")

        recovered = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0), self.inverter_sample(14, 0)],
            self.inverter_rule(), pending,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T10:13:00+08:00")
        self.assertEqual(recovered["last_seen_time"], "2026-08-25T10:14:00+08:00")

    def test_active_inverter_event_recovery_history_gap_resets_candidate(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        pending = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0)], self.inverter_rule(), active,
        )
        gapped = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0), self.inverter_sample(15, 0)],
            self.inverter_rule(), pending,
        )
        self.assertEqual(gapped["status"], "active")
        self.assertIsNone(gapped["end_time"])
        self.assertEqual(gapped["last_seen_time"], "2026-08-25T10:15:00+08:00")

    def test_active_inverter_event_old_recovery_window_does_not_advance_or_recover(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        pending = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0)], self.inverter_rule(), active,
        )
        replayed = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10),
             self.inverter_sample(12, 12), self.inverter_sample(13, 0)],
            self.inverter_rule(), pending,
        )
        self.assertEqual(replayed["status"], "active")
        self.assertIsNone(replayed["end_time"])
        self.assertEqual(replayed["last_seen_time"], "2026-08-25T10:13:00+08:00")

    def test_active_inverter_event_recovery_run_must_end_with_new_sample(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        replayed_state = dict(active, last_seen_time="2026-08-25T10:14:00+08:00")
        replayed = evaluate_inverter_low_load(
            [self.inverter_sample(13, 0), self.inverter_sample(14, 0)],
            self.inverter_rule(), replayed_state,
        )
        self.assertEqual(replayed["status"], "active")
        self.assertIsNone(replayed["end_time"])
        self.assertEqual(replayed["last_seen_time"], "2026-08-25T10:14:00+08:00")

    def test_active_inverter_event_rejects_identity_or_status_mismatch(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(10, 15), self.inverter_sample(11, 10), self.inverter_sample(12, 12)],
            self.inverter_rule(),
        )
        for field, value in (
            ("station_id", "station-2"),
            ("event_type", "battery_temperature_rise"),
            ("device_id", "inv-2"),
            ("status", "recovered"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(HistoryError) as caught:
                    evaluate_inverter_low_load(
                        [self.inverter_sample(13, 10)], self.inverter_rule(), dict(active, **{field: value}),
                    )
                self.assertEqual(caught.exception.code, "invalid_active_event")

    def test_function_entries_reject_none_boolean_or_blank_identifiers(self):
        chains = {
            "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
            "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
            "pv_load": {"efficiency": 95, "input_kw": 100, "output_kw": 95},
        }
        for value in (None, True, "   "):
            with self.subTest(field="station_id", value=value), self.assertRaises(HistoryError):
                build_minute_point(value, "2026-08-25T10:00:00+08:00", chains, "v1", "2026-08-25T10:00:01+08:00")
            with self.subTest(field="formula_version", value=value), self.assertRaises(HistoryError):
                build_minute_point("station-1", "2026-08-25T10:00:00+08:00", chains, value, "2026-08-25T10:00:01+08:00")
            with self.subTest(field="rule.station_id", value=value), self.assertRaises(HistoryError):
                normalize_rule(dict(self.inverter_rule(), station_id=value))
            for evaluator, sample in (
                (evaluate_inverter_low_load, self.inverter_sample(0, 10)),
                (evaluate_battery_temperature_rise, self.temperature_sample(0, 25)),
            ):
                for device_field in ("device_id", "device_name"):
                    with self.subTest(field=device_field, value=value, evaluator=evaluator.__name__), self.assertRaises(HistoryError):
                        evaluator(
                            [dict(sample, **{device_field: value})], self.inverter_rule(),
                        )

    def test_inverter_event_recovers_when_device_stops_for_configured_duration(self):
        active = evaluate_inverter_low_load(
            [self.inverter_sample(0, 15), self.inverter_sample(1, 10), self.inverter_sample(2, 12)],
            self.inverter_rule(),
        )
        samples = [self.inverter_sample(3, 0), self.inverter_sample(4, 0)]
        recovered = evaluate_inverter_low_load(samples, self.inverter_rule(), active)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T10:03:00+08:00")
        self.assertEqual(recovered["last_seen_time"], "2026-08-25T10:04:00+08:00")

    def test_inverter_gap_does_not_complete_trigger_or_recovery(self):
        samples = [self.inverter_sample(0, 10), self.inverter_sample(2, 10), self.inverter_sample(3, 10)]
        self.assertIsNone(evaluate_inverter_low_load(samples, self.inverter_rule()))

    def test_chain_low_efficiency_opens_after_two_minutes_with_snapshot(self):
        snapshot = {"battery_cabinets": [{"device_id": "emu21", "temperature_c": None}]}
        event = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)],
            self.inverter_rule(),
            trigger_device_snapshot=snapshot,
            diagnosed_causes=[],
        )
        self.assertEqual(event["event_type"], "chain_low_efficiency")
        self.assertEqual(event["start_time"], "2026-08-27T10:00:00+08:00")
        self.assertEqual(event["observed_value"], 80.0)
        self.assertEqual(event["evidence"]["cause_status"], "pending")
        self.assertEqual(event["evidence"]["trigger_device_snapshot"], snapshot)
        self.assertEqual(
            event["evidence"]["confirmation_time"],
            "2026-08-27T10:01:00+08:00",
        )

    def test_chain_event_recovers_after_two_minutes_at_or_above_85(self):
        active = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
            trigger_device_snapshot={}, diagnosed_causes=[],
        )
        recovered = evaluate_chain_low_efficiency(
            [self.chain_sample(1, 82), self.chain_sample(2, 85), self.chain_sample(3, 86)],
            self.inverter_rule(), active_event=active,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-27T10:02:00+08:00")

    def test_chain_event_canonicalizes_utc_samples_to_shanghai_time(self):
        utc_samples = [
            dict(self.chain_sample(0, 80), data_time="2026-08-27T02:00:00Z"),
            dict(self.chain_sample(1, 82), data_time="2026-08-27T02:01:00Z"),
        ]
        active = evaluate_chain_low_efficiency(
            utc_samples, self.inverter_rule(), trigger_device_snapshot={}, diagnosed_causes=[],
        )
        self.assertEqual(active["start_time"], "2026-08-27T10:00:00+08:00")
        self.assertEqual(active["last_seen_time"], "2026-08-27T10:01:00+08:00")

        recovered = evaluate_chain_low_efficiency(
            [
                dict(self.chain_sample(1, 82), data_time="2026-08-27T02:01:00Z"),
                dict(self.chain_sample(2, 85), data_time="2026-08-27T02:02:00Z"),
                dict(self.chain_sample(3, 86), data_time="2026-08-27T02:03:00Z"),
            ],
            self.inverter_rule(), active_event=active,
        )
        self.assertEqual(recovered["end_time"], "2026-08-27T10:02:00+08:00")
        self.assertEqual(recovered["last_seen_time"], "2026-08-27T10:03:00+08:00")

    def test_chain_missing_minute_does_not_complete_trigger(self):
        self.assertIsNone(evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(2, 82)], self.inverter_rule(),
        ))

    def test_chain_null_efficiency_does_not_complete_trigger(self):
        self.assertIsNone(evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, None)], self.inverter_rule(),
        ))

    def test_active_chain_event_with_no_valid_samples_returns_unchanged(self):
        active = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
            trigger_device_snapshot={}, diagnosed_causes=[],
        )
        unchanged = evaluate_chain_low_efficiency(
            [self.chain_sample(2, None)], self.inverter_rule(), active_event=active,
        )
        self.assertEqual(unchanged, active)
        self.assertIsNot(unchanged, active)

    def test_active_chain_event_recovers_after_two_zero_input_minutes(self):
        active = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
            trigger_device_snapshot={}, diagnosed_causes=[],
        )
        stopped = [
            dict(self.chain_sample(2, None), input_kw=0),
            dict(self.chain_sample(3, None), input_kw=0),
        ]

        pending = evaluate_chain_low_efficiency(
            stopped[:1], self.inverter_rule(), active_event=active,
        )
        recovered = evaluate_chain_low_efficiency(
            stopped, self.inverter_rule(), active_event=active,
        )

        self.assertEqual(pending["status"], "active")
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-27T10:02:00+08:00")
        self.assertEqual(recovered["last_seen_time"], "2026-08-27T10:03:00+08:00")
        self.assertEqual(recovered["evidence"]["recovery_reason"], "chain_stopped")

    def test_active_chain_event_does_not_overwrite_trigger_snapshot(self):
        original = {"pv_inverters": [{"device_id": "emu1", "load_rate_pct": 10.0}]}
        active = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)], self.inverter_rule(),
            trigger_device_snapshot=original, diagnosed_causes=[],
        )
        updated = evaluate_chain_low_efficiency(
            [self.chain_sample(2, 70)], self.inverter_rule(), active_event=active,
            trigger_device_snapshot={"pv_inverters": []},
        )
        self.assertEqual(updated["evidence"]["trigger_device_snapshot"], original)
        self.assertEqual(active["evidence"]["trigger_device_snapshot"], original)

    def test_temperature_event_uses_window_rise_and_opens_at_first_condition(self):
        samples = [
            self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
            self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
            self.temperature_sample(6, 28.5),
        ]
        event = evaluate_battery_temperature_rise(samples, self.inverter_rule())
        self.assertEqual(event["start_time"], "2026-08-25T11:05:00+08:00")
        self.assertEqual(event["event_type"], "battery_temperature_rise")
        self.assertAlmostEqual(event["observed_value"], 3.2)
        self.assertEqual(event["impact_chain"], ["光→储", "储→用"])

    def test_temperature_event_exposes_structured_rise_evidence(self):
        event = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        evidence = event["evidence"]
        self.assertEqual(evidence["current_temperature_c"], 28.5)
        self.assertEqual(evidence["window_start_temperature_c"], 25.3)
        self.assertAlmostEqual(evidence["current_rise_c"], 3.2)
        self.assertAlmostEqual(evidence["maximum_rise_c"], 3.2)
        self.assertEqual(evidence["window_minutes"], 5)
        self.assertEqual(evidence["threshold_c"], 3.0)
        self.assertEqual(evidence["continuous_minutes"], 2)
        self.assertEqual(
            evidence["confirmation_time"],
            "2026-08-25T11:06:00+08:00",
        )
        self.assertEqual(evidence["rule"]["version"], 4)
        self.assertEqual(evidence["display_text"], "5 分钟最大温升 3.20℃")

    def test_legacy_battery_snapshot_without_chain_fields_stays_active_and_recovers(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        legacy_rule = {
            key: value for key, value in active["evidence"]["rule"].items()
            if not key.startswith("chain_low_efficiency_")
            and not key.startswith("inverter_")
        }
        legacy = {**active, "evidence": {**active["evidence"], "rule": legacy_rule}}
        changed_current_rule = dict(
            self.inverter_rule(),
            temperature_rise_threshold_c=100,
            temperature_recovery_minutes=9,
            version=5,
        )

        continued = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5), self.temperature_sample(7, 29.0),
            ],
            changed_current_rule,
            legacy,
        )
        self.assertEqual(continued["status"], "active")
        self.assertEqual(continued["evidence"]["continuous_minutes"], 3)
        self.assertEqual(continued["evidence"]["rule"], legacy_rule)

        recovered = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5), self.temperature_sample(7, 28.0),
                self.temperature_sample(8, 28.2),
            ],
            changed_current_rule,
            legacy,
        )
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T11:07:00+08:00")

    def test_temperature_event_recovers_after_consecutive_below_threshold_windows(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        recovery_samples = [
            self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
            self.temperature_sample(6, 28.5), self.temperature_sample(7, 28.0),
            self.temperature_sample(8, 28.2),
        ]
        recovered = evaluate_battery_temperature_rise(recovery_samples, self.inverter_rule(), active)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["end_time"], "2026-08-25T11:07:00+08:00")

    def test_temperature_window_gap_is_not_a_valid_condition(self):
        samples = [
            self.temperature_sample(0, 25.0),
            self.temperature_sample(2, 25.5),
            self.temperature_sample(3, 26.0),
            self.temperature_sample(4, 26.5),
            self.temperature_sample(5, 29.0),
            self.temperature_sample(6, 29.5),
        ]
        self.assertIsNone(evaluate_battery_temperature_rise(samples, self.inverter_rule()))

    def test_active_temperature_event_uses_snapshot_and_deduplicates_overlap(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        disabled = dict(self.inverter_rule(), enabled=False, version=5)
        expanded = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5), self.temperature_sample(7, 29.0),
            ],
            disabled, active,
        )
        self.assertEqual(expanded["evidence"]["continuous_minutes"], 3)
        self.assertEqual(expanded["evidence"]["rule"]["version"], 4)

        repeated = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5), self.temperature_sample(7, 29.0),
            ],
            disabled, expanded,
        )
        self.assertEqual(repeated["evidence"]["continuous_minutes"], 3)

    def test_temperature_missing_window_does_not_recover_or_update_last_seen(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        unchanged = evaluate_battery_temperature_rise(
            [self.temperature_sample(7, 28.0), self.temperature_sample(8, 28.2)],
            self.inverter_rule(), active,
        )
        self.assertEqual(unchanged["status"], "active")
        self.assertEqual(unchanged["last_seen_time"], "2026-08-25T11:06:00+08:00")
        self.assertEqual(unchanged["evidence"]["continuous_minutes"], 2)

    def test_active_temperature_event_rejects_identity_and_state_before_incomplete_window(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        for field, value in (
            ("station_id", "station-2"),
            ("event_type", "inverter_low_load"),
            ("device_id", "battery-2"),
            ("device_name", "2#电池簇"),
            ("status", "recovered"),
            ("end_time", "2026-08-25T11:07:00+08:00"),
            ("last_seen_time", "2026-08-25T11:04:00+08:00"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(HistoryError) as caught:
                    evaluate_battery_temperature_rise(
                        [self.temperature_sample(0, 25.0)],
                        self.inverter_rule(), dict(active, **{field: value}),
                    )
                self.assertEqual(caught.exception.code, "invalid_active_event")

    def test_active_temperature_event_rejects_missing_or_invalid_snapshot_before_early_return(self):
        active = evaluate_battery_temperature_rise(
            [
                self.temperature_sample(0, 25.0), self.temperature_sample(1, 25.3),
                self.temperature_sample(2, 25.6), self.temperature_sample(3, 26.0),
                self.temperature_sample(4, 26.5), self.temperature_sample(5, 28.1),
                self.temperature_sample(6, 28.5),
            ],
            self.inverter_rule(),
        )
        invalid_snapshot = dict(
            active["evidence"]["rule"], temperature_trigger_minutes=0,
        )
        for evidence in (
            {},
            dict(active["evidence"], rule=invalid_snapshot),
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaises(HistoryError) as caught:
                    evaluate_battery_temperature_rise([], self.inverter_rule(), dict(active, evidence=evidence))
                self.assertEqual(caught.exception.code, "invalid_active_event")

    def test_calendar_day_dashboard_uses_existing_minutes_only(self):
        points = [
            {
                "station_id": "station-1",
                "data_time": "2026-08-25T00:00:00+08:00",
                "pv_storage_efficiency": 50.0,
                "storage_load_efficiency": None,
                "pv_load_efficiency": 80.0,
                "pv_storage_input_kw": 100.0,
                "pv_storage_output_kw": 50.0,
                "storage_load_input_kw": None,
                "storage_load_output_kw": None,
                "pv_load_input_kw": 100.0,
                "pv_load_output_kw": 80.0,
                "formula_version": "v1",
                "calculated_at": "2026-08-25T00:00:02+08:00",
            },
            {
                "station_id": "station-1",
                "data_time": "2026-08-25T00:02:00+08:00",
                "pv_storage_efficiency": 100.0,
                "storage_load_efficiency": 90.0,
                "pv_load_efficiency": None,
                "pv_storage_input_kw": 300.0,
                "pv_storage_output_kw": 300.0,
                "storage_load_input_kw": 100.0,
                "storage_load_output_kw": 90.0,
                "pv_load_input_kw": None,
                "pv_load_output_kw": None,
                "formula_version": "v1",
                "calculated_at": "2026-08-25T00:02:02+08:00",
            },
        ]
        events = [{
            "id": 7,
            "station_id": "station-1",
            "event_type": "inverter_low_load",
            "device_id": "inv-1",
            "device_name": "1#逆变器",
            "start_time": "2026-08-24T23:50:00+08:00",
            "end_time": None,
            "last_seen_time": "2026-08-25T00:02:00+08:00",
            "status": "active",
            "observed_value": 12.0,
            "threshold_value": 20.0,
            "observed_unit": "%",
            "evidence": {"display_text": "负载率最低 12.0%"},
            "impact_chain": ["光→储", "光→用"],
            "rule_version": 3,
        }]
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {"inputs": {"data_time": "2026-08-25T14:36:20+08:00"}, "result": {}},
            list(reversed(points)), events,
        )
        self.assertEqual(result["range"]["start_time"], "2026-08-25T00:00:00+08:00")
        self.assertEqual(result["range"]["end_time"], "2026-08-26T00:00:00+08:00")
        self.assertEqual(result["range"]["latest_time"], "2026-08-25T00:02:00+08:00")
        self.assertEqual([row["data_time"] for row in result["trend"]], [
            "2026-08-25T00:00:00+08:00", "2026-08-25T00:02:00+08:00"
        ])
        self.assertAlmostEqual(result["summary_today"]["pv_storage_efficiency"], 87.5)
        self.assertAlmostEqual(result["summary_today"]["storage_load_efficiency"], 90.0)
        self.assertAlmostEqual(result["summary_today"]["pv_load_efficiency"], 80.0)
        self.assertEqual(result["events"][0]["status"], "持续中")
        self.assertIsNone(result["events"][0]["end"])

    def test_calendar_day_dashboard_returns_empty_contract_without_points(self):
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, [], [],
        )
        self.assertIsNone(result["range"]["latest_time"])
        self.assertEqual(result["trend"], [])
        self.assertIsNone(result["summary_today"]["pv_storage_efficiency"])
        self.assertEqual(result["events"], [])

    def test_calendar_day_dashboard_publishes_chain_cause_contract(self):
        event = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)],
            self.inverter_rule(),
            trigger_device_snapshot={
                "battery_cabinets": [{"device_id": "emu21", "temperature_c": None}],
            },
            diagnosed_causes=[],
        )
        dashboard = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-27T14:36:20+08:00",
            {}, [], [event],
        )
        self.assertEqual(dashboard["events"][0]["type"], "链路低效率")
        self.assertEqual(dashboard["events"][0]["device"], "原因待判断")
        self.assertEqual(dashboard["events"][0]["cause_status"], "pending")
        self.assertEqual(
            dashboard["events"][0]["trigger_device_snapshot"]["battery_cabinets"][0]["temperature_c"],
            None,
        )

    def test_calendar_day_dashboard_uses_diagnosed_devices_for_chain_event(self):
        event = evaluate_chain_low_efficiency(
            [self.chain_sample(0, 80), self.chain_sample(1, 82)],
            self.inverter_rule(),
            trigger_device_snapshot={},
            diagnosed_causes=[
                {
                    "event_type": "inverter_low_load",
                    "device_id": "emu1",
                    "device_name": "光伏逆变器 emu1",
                },
                {
                    "event_type": "battery_temperature_rise",
                    "device_id": "emu21",
                    "device_name": "电池柜 emu21",
                },
            ],
        )

        dashboard = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-27T14:36:20+08:00",
            {}, [], [event],
        )

        self.assertEqual(
            dashboard["events"][0]["device"],
            "光伏逆变器 emu1、电池柜 emu21",
        )

    def test_calendar_day_dashboard_rejects_duplicates_and_excludes_future_points(self):
        point = build_minute_point(
            "station-1", "2026-08-25T14:36:00+08:00",
            {
                "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 95, "input_kw": 100, "output_kw": 95},
            },
            "v1", "2026-08-25T14:36:02+08:00",
        )
        with self.assertRaises(HistoryError) as caught:
            build_calendar_day_dashboard(
                "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
                {}, [point, dict(point)], [],
            )
        self.assertEqual(caught.exception.code, "duplicate_minute")

        future = dict(point, data_time="2026-08-25T14:37:00+08:00")
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, [point, future], [],
        )
        self.assertEqual(len(result["trend"]), 1)

    def test_build_minute_point_floors_seconds_and_preserves_independent_nulls(self):
        point = build_minute_point(
            station_id="station-1",
            data_time="2026-08-25T14:36:47+08:00",
            chains={
                "pv_storage": {"efficiency": 91.5, "input_kw": 103, "output_kw": 95},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 94.3, "input_kw": 96, "output_kw": 90.528},
            },
            formula_version="energy-chain-v1",
            calculated_at="2026-08-25T14:36:49+08:00",
        )
        self.assertEqual(point["data_time"], "2026-08-25T14:36:00+08:00")
        self.assertEqual(point["pv_storage_efficiency"], 91.5)
        self.assertIsNone(point["storage_load_efficiency"])
        self.assertEqual(point["pv_load_output_kw"], 90.528)

    def test_build_minute_point_rejects_naive_time_and_invalid_numbers(self):
        valid_chains = {
            name: {"efficiency": None, "input_kw": None, "output_kw": None}
            for name in ("pv_storage", "storage_load", "pv_load")
        }
        with self.assertRaises(HistoryError) as caught:
            build_minute_point(
                "station-1", "2026-08-25T14:36:00", valid_chains,
                "energy-chain-v1", "2026-08-25T14:36:01+08:00",
            )
        self.assertEqual(caught.exception.code, "invalid_time")

        valid_chains["pv_storage"]["input_kw"] = -1
        with self.assertRaises(HistoryError) as caught:
            build_minute_point(
                "station-1", "2026-08-25T14:36:00+08:00", valid_chains,
                "energy-chain-v1", "2026-08-25T14:36:01+08:00",
            )
        self.assertEqual(caught.exception.code, "invalid_minute_point")

    def test_build_minute_upsert_uses_station_and_minute_as_key(self):
        point = build_minute_point(
            "station-1",
            "2026-08-25T14:36:00+08:00",
            {
                "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 95, "input_kw": 80, "output_kw": 76},
            },
            "energy-chain-v1",
            "2026-08-25T14:36:02+08:00",
        )
        envelope = build_minute_upsert(point)
        self.assertEqual(envelope["collection"], "t_efficiency_points")
        self.assertEqual(
            envelope["key"],
            {"station_id": "station-1", "data_time": "2026-08-25T06:36:00+00:00"},
        )
        self.assertEqual(envelope["values"], point)

    def test_full_fixture_matches_html_contract(self):
        from m2.tests.station_efficiency_history_test_support import build_history_dashboard_response

        payload = build_history_dashboard_response()
        self.assertEqual(payload["operation"], "dashboard")
        self.assertEqual(payload["range"]["latest_time"], "2026-08-25T14:36:00+08:00")
        self.assertEqual(len(payload["trend"]), 7)
        self.assertEqual([row["data_time"] for row in payload["trend"]], [
            "2026-08-25T14:27:00+08:00", "2026-08-25T14:28:00+08:00",
            "2026-08-25T14:29:00+08:00", "2026-08-25T14:33:00+08:00",
            "2026-08-25T14:34:00+08:00", "2026-08-25T14:35:00+08:00",
            "2026-08-25T14:36:00+08:00",
        ])
        self.assertIsNone(payload["trend"][1]["storageLoad"])
        self.assertEqual(payload["trend"][1]["data_time"], "2026-08-25T14:28:00+08:00")
        self.assertEqual(
            [payload["trend"][0]["data_time"], payload["trend"][2]["data_time"]],
            ["2026-08-25T14:27:00+08:00", "2026-08-25T14:29:00+08:00"],
        )
        self.assertEqual(payload["trend"][3]["data_time"], "2026-08-25T14:33:00+08:00")
        self.assertEqual(payload["realtime"]["inputs"]["data_time"], payload["range"]["latest_time"])
        self.assertEqual(payload["realtime"]["result"]["data_time"], payload["range"]["latest_time"])
        self.assertEqual([event["start"] for event in payload["events"]], [
            "2026-08-24T23:50:00+08:00", "2026-08-25T14:30:00+08:00",
        ])
        self.assertEqual([event["event_type"] for event in payload["events"]], [
            "battery_temperature_rise", "inverter_low_load",
        ])
        self.assertEqual([event["status"] for event in payload["events"]], ["已恢复", "持续中"])
        self.assertEqual(payload["events"][0]["end"], "2026-08-25T00:10:00+08:00")
        self.assertIsNone(payload["events"][1]["end"])

    def test_calendar_day_dashboard_canonicalizes_utc_minutes_to_station_timezone(self):
        points = [
            build_minute_point(
                "station-1", "2026-08-24T16:00:47Z",
                {
                    "pv_storage": {"efficiency": 50, "input_kw": 100, "output_kw": 50},
                    "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                    "pv_load": {"efficiency": 80, "input_kw": 100, "output_kw": 80},
                },
                "v1", "2026-08-24T16:00:49Z",
            ),
            build_minute_point(
                "station-1", "2026-08-25T00:02:47Z",
                {
                    "pv_storage": {"efficiency": 100, "input_kw": 300, "output_kw": 300},
                    "storage_load": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                    "pv_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                },
                "v1", "2026-08-25T00:02:49Z",
            ),
        ]
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, points, [],
        )
        self.assertEqual(result["range"]["latest_time"], "2026-08-25T08:02:00+08:00")
        self.assertEqual(result["summary_today"]["end_time"], "2026-08-25T08:02:00+08:00")
        self.assertEqual([row["data_time"] for row in result["trend"]], [
            "2026-08-25T00:00:00+08:00", "2026-08-25T08:02:00+08:00",
        ])
        self.assertAlmostEqual(result["summary_today"]["pv_storage_efficiency"], 87.5)

    def test_calendar_day_dashboard_rejects_equivalent_absolute_minute_duplicates(self):
        point = build_minute_point(
            "station-1", "2026-08-25T00:00:00+08:00",
            {
                "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
                "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
                "pv_load": {"efficiency": 95, "input_kw": 100, "output_kw": 95},
            },
            "v1", "2026-08-25T00:00:02+08:00",
        )
        equivalent = dict(point, data_time="2026-08-24T16:00:00Z")
        with self.assertRaises(HistoryError) as caught:
            build_calendar_day_dashboard(
                "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
                {}, [point, equivalent], [],
            )
        self.assertEqual(caught.exception.code, "duplicate_minute")

    def test_calendar_day_dashboard_canonicalizes_event_times_to_station_timezone(self):
        event = {
            "id": 8,
            "station_id": "station-1",
            "event_type": "battery_temperature_rise",
            "device_id": "battery-1",
            "device_name": "1#电池簇",
            "start_time": "2026-08-24T16:00:00Z",
            "end_time": "2026-08-24T16:10:00Z",
            "status": "recovered",
            "evidence": {"display_text": "5 分钟最大温升 3.50℃"},
            "impact_chain": ["光→储", "储→用"],
        }
        result = build_calendar_day_dashboard(
            "station-1", "Asia/Shanghai", "2026-08-25T14:36:20+08:00",
            {}, [], [event],
        )
        self.assertEqual(result["events"][0]["start"], "2026-08-25T00:00:00+08:00")
        self.assertEqual(result["events"][0]["end"], "2026-08-25T00:10:00+08:00")

    def test_build_minute_upsert_canonicalizes_equivalent_absolute_minute_keys(self):
        chains = {
            "pv_storage": {"efficiency": 90, "input_kw": 100, "output_kw": 90},
            "storage_load": {"efficiency": None, "input_kw": None, "output_kw": None},
            "pv_load": {"efficiency": 95, "input_kw": 100, "output_kw": 95},
        }
        local_point = build_minute_point(
            "station-1", "2026-08-25T00:00:00+08:00", chains, "v1", "2026-08-25T00:00:02+08:00",
        )
        utc_point = build_minute_point(
            "station-1", "2026-08-24T16:00:00Z", chains, "v1", "2026-08-24T16:00:02Z",
        )
        self.assertEqual(
            build_minute_upsert(local_point)["key"],
            build_minute_upsert(utc_point)["key"],
        )
