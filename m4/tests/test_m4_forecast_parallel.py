"""Forecast and accuracy must come from one current-task read."""
from unittest import TestCase
from unittest.mock import patch
from m4.settings import forecast_source as source
from m4.tests.test_m4_forecast_source import Client, DAY, run

class CurrentTaskReadTests(TestCase):
    def test_forecasts_and_gate_share_one_read(self):
        client=Client([run(1,DAY)])
        result=source.load_forecast(client,'station-1',plan_start_at=DAY,now=DAY,require_full_day=True)
        self.assertEqual(client.calls,[('current_load_result','ES01')])
        self.assertEqual(result['coverage_points'],96)
        self.assertEqual(result['accuracy_gate']['status'],'ready')
        self.assertEqual(result['run_id'],result['accuracy_gate']['evidence']['run']['run_id'])

    def test_invalid_station_never_starts_upstream_reads(self):
        with patch.object(source,'read_gate') as gate:
            with self.assertRaises(ValueError):source.load_forecast(Client([]),'invalid',plan_start_at=DAY,now=DAY)
        gate.assert_not_called()

    def test_missing_score_blocks_without_discarding_forecasts(self):
        client=Client([run(1,DAY)])
        evidence=client.current_load_result('ES01');evidence.pop('current_score')
        with patch.object(client,'current_load_result',return_value=evidence) as read:
            result=source.load_forecast(client,'station-1',plan_start_at=DAY,now=DAY,require_full_day=True)
        read.assert_called_once_with('ES01')
        self.assertEqual(result['coverage_points'],96)
        self.assertEqual(result['accuracy_gate']['status'],'unavailable')
        self.assertTrue(result['issues'])

    def test_read_failure_never_falls_back_to_legacy_tables(self):
        client=Client([run(1,DAY)])
        with patch.object(client,'current_load_result',side_effect=ConnectionError),patch.object(client,'list_rows') as legacy:
            with self.assertRaises(ValueError):source.load_forecast(client,'station-1',plan_start_at=DAY,now=DAY)
        legacy.assert_not_called()
