"""Regression coverage for JSON inputs crossing the rolling request boundary."""
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from m4.settings.rolling_plans import prepare_remaining_request
from m4.tests.m4_optimizer_test_support import make_request


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
