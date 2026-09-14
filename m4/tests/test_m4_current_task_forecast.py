import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from m4.settings.forecast_source import load_forecast, validate_forecast_values
from m4.tests.test_m4_load_accuracy import NOW, row


class CurrentTaskForecastTests(unittest.TestCase):
    def read(self, evidence, start=None):
        client = Mock()
        client.current_load_result.return_value = evidence
        client.list_rows.side_effect = AssertionError('must not mix other forecasts')
        start = start or datetime.fromisoformat(evidence['run']['forecast_start'])
        source = load_forecast(client, 'station-2', plan_start_at=start,
                               now=NOW, require_full_day=start.hour == 0)
        client.current_load_result.assert_called_once_with('ES02')
        client.list_rows.assert_not_called()
        return source

    def test_forecast_and_score_share_one_task(self):
        evidence = row('10')
        evidence['points'][56]['forecast_value'] = 321.5
        source = self.read(evidence)
        self.assertEqual(source['values'][56], 321.5)
        self.assertEqual(source['run_id'], source['accuracy_gate']['run_id'])
        self.assertEqual(source['coverage_points'], 96)
        self.assertEqual(source['actual_values'][0], 100)
        self.assertIsNone(source['actual_values'][56])

    def test_actual_refresh_does_not_replace_forecast_or_version(self):
        evidence = row('10')
        before = self.read(evidence)
        updated = deepcopy(evidence)
        updated['points'][1].update(actual_value=77, actual_quality='valid')
        updated['current_score'].update(actual_count=2, valid_count=2)
        after = self.read(updated)
        self.assertEqual(before['values'], after['values'])
        self.assertEqual(before['version'], after['version'])
        self.assertEqual(after['actual_values'][1], 77)
        self.assertNotEqual(before['accuracy_gate']['version'], after['accuracy_gate']['version'])

    def test_missing_future_does_not_fall_back(self):
        evidence = row('10')
        start = datetime.fromisoformat(evidence['run']['forecast_start']) + timedelta(hours=12)
        source = self.read(evidence, start)
        self.assertEqual(source['coverage_points'], 48)
        self.assertIsNone(source['values'][-1])
        self.assertTrue(source['issues'])

    def test_invalid_or_duplicate_points_rejected(self):
        for change in ('duplicate', 'negative', 'station'):
            evidence = row('10')
            if change == 'duplicate':
                evidence['points'][1] = deepcopy(evidence['points'][0])
            elif change == 'negative':
                evidence['points'][2]['forecast_value'] = -1
            else:
                evidence['run']['station_id'] = 'ES01'
            with self.assertRaises(ValueError):
                self.read(evidence)

    def test_solver_input_cannot_diverge_from_scored_task(self):
        evidence = row('10')
        source = self.read(evidence)
        points = [SimpleNamespace(timestamp=datetime.fromisoformat(p['target_time']),
                                 load_forecast_kw=p['forecast_value']) for p in evidence['points']]
        validate_forecast_values(source, points)
        points[56].load_forecast_kw += 1
        with self.assertRaises(ValueError):
            validate_forecast_values(source, points)
