"""Regression coverage for JSON inputs crossing the rolling request boundary."""
from datetime import timedelta
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from m4.settings.rolling_plans import prepare_remaining_request, RollingPlanService
from m4.tests.m4_optimizer_test_support import make_request
from m4.settings.pv_correction import correct_forecast


class RollingRequestTests(unittest.TestCase):
    def setUp(self):
        self.base = make_request(station_id='station-1')
        self.now = self.base.plan_start_at + timedelta(hours=12, minutes=1)
        self.configuration = SimpleNamespace(station_id='station-1',
            parameters=SimpleNamespace(max_input_age_seconds=900))
        sample = dict(station_id='station-1', status='ready', observed_at=self.now.isoformat())
        self.inputs = dict(station_id='station-1', can_compare=True,
            request=self.base.model_dump(mode='json'), baseline=dict(schedule=[dict(
                start_time='00:00:00', end_time='06:00:00', repeat='daily',
                mode='charge', power_kw=20.0)]),
            sources=dict(current_soc=dict(sample, soc_pct=27.6),
                current_power=dict(sample, power_kw=0.0), load=dict(accuracy_gate={})))

    def prepare(self):
        # Accuracy scoring is independent of JSON deserialization; no upstream I/O.
        with patch('m4.settings.rolling_plans.require_gate'):
            return prepare_remaining_request(self.configuration, self.inputs, self.now)

    def test_json_daily_request_becomes_state_anchored_remaining_day(self):
        request, replay, _, anchor, _ = self.prepare()
        self.assertEqual(request.plan_start_at, self.now.replace(minute=15))
        self.assertEqual(request.input_observed_at, self.now)
        self.assertEqual(request.horizon_points, 47)
        self.assertEqual(len(replay), 47)
        self.assertEqual(request.points[0].timestamp, request.plan_start_at)
        self.assertEqual(request.points[-1].timestamp + timedelta(minutes=15),
            self.base.plan_start_at + timedelta(days=1))
        self.assertEqual(request.capability.initial_soc_pct, 27.6)
        self.assertEqual(anchor['measured_soc_pct'], 27.6)
        self.assertEqual(self.inputs['request'], self.base.model_dump(mode='json'))

    def test_json_boundary_still_rejects_numeric_strings(self):
        self.inputs['request']['points'][0]['load_forecast_kw'] = '100.0'
        with self.assertRaises(ValidationError):
            self.prepare()

    def test_json_boundary_still_rejects_invalid_timestamps(self):
        self.inputs['request']['points'][0]['timestamp'] = 'invalid'
        with self.assertRaises(ValidationError):
            self.prepare()

    def test_pv_correction_applies_to_both_comparison_inputs_without_mutation(self):
        history = [dict(timestamp=self.now.replace(minute=0)-timedelta(minutes=15*i),
            forecast_kw=20., actual_kw=10., valid_minutes=15) for i in range(1, 5)]
        future = [dict(timestamp=p.timestamp, forecast_kw=p.pv_forecast_kw)
            for p in self.base.points if p.timestamp > self.now]
        correction = correct_forecast(history, future, self.now)
        correction.update(station_id='station-1', source_version='pv-test', version='correction-test')
        self.inputs['sources'].update(pv=dict(version='pv-test'), pv_correction=correction)
        request, baseline, _, _, _ = self.prepare()
        self.assertEqual(request.points[0].pv_forecast_kw, 10.)
        self.assertEqual(request.source_versions['pv_correction'], 'correction-test')
        self.assertEqual(self.inputs['request'], self.base.model_dump(mode='json'))
        # EMS and optimizer use the same corrected net load (idle EMS at noon).
        self.assertAlmostEqual(baseline[0].grid_import_kw, 90.)
        correction['points'][0]['solver_kw'] = 25.
        with self.assertRaises(ValueError): self.prepare()

    def test_unreadable_optional_pv_measurements_keep_original_forecast(self):
        self.inputs['sources']['pv_correction'] = dict(status='read_failed')
        request, _, _, _, _ = self.prepare()
        self.assertEqual(request.points[0].pv_forecast_kw, 20.)

    def test_old_policy_becomes_stale_without_breaking_daily_read(self):
        with tempfile.TemporaryDirectory() as directory:
            daily = SimpleNamespace(root=Path(directory), _path=lambda station: None)
            service = RollingPlanService(daily)
            service.root.mkdir()
            (service.root/'station-1.json').write_text(json.dumps(dict(station_id='station-1',
                policy_version='remaining-day-v1', status='completed')))
            self.assertEqual(service.latest('station-1')['status'], 'stale')
