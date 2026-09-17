"""Causal PV calibration: no future labels, no single-cloud extrapolation."""
from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import Mock

from m4.settings.pv_correction import correct_forecast, read_correction


class PVCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 16, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.history = [dict(timestamp=self.now-timedelta(minutes=60-15*i),
            forecast_kw=100., actual_kw=150., valid_minutes=15) for i in range(4)]
        self.future = [dict(timestamp=self.now+timedelta(minutes=15*i),
            forecast_kw=100.) for i in range(10)]

    def test_persistent_bias_capped_then_decays(self):
        r = correct_forecast(self.history, self.future, self.now)
        self.assertEqual(r['status'], 'applied')
        self.assertAlmostEqual(r['points'][0]['corrected_kw'], 130.)
        self.assertEqual(r['points'][0]['solver_kw'], 100.)
        self.assertEqual(r['points'][8]['corrected_kw'], 100.)
        self.assertGreater(r['points'][5]['corrected_kw'], r['points'][7]['corrected_kw'])
        self.assertEqual(self.future[0]['forecast_kw'], 100.)

    def test_future_or_unfinished_observation_never_used(self):
        dirty = self.history+[dict(timestamp=self.now, forecast_kw=100., actual_kw=0., valid_minutes=15)]
        self.assertEqual(correct_forecast(dirty, self.future, self.now),
                         correct_forecast(self.history, self.future, self.now))

    def test_one_cloud_event_is_not_persistent_bias(self):
        for p in self.history: p['actual_kw'] = 100.
        self.history[-1]['actual_kw'] = 0.
        self.assertEqual(correct_forecast(self.history, self.future, self.now)['status'], 'unchanged')

    def test_missing_stale_and_low_light_fall_back(self):
        for p in self.history: p['valid_minutes'] = 5
        self.assertEqual(correct_forecast(self.history, self.future, self.now)['status'], 'unavailable')
        for p in self.history: p.update(valid_minutes=15, forecast_kw=1.)
        self.assertEqual(correct_forecast(self.history, self.future, self.now)['status'], 'unavailable')

    def test_negative_bias_is_nonnegative_and_no_night_generation(self):
        for p in self.history: p['actual_kw'] = 0.
        self.future[-1]['forecast_kw'] = 0.
        r = correct_forecast(self.history, self.future, self.now)
        self.assertEqual(r['points'][0]['corrected_kw'], 70.)
        self.assertEqual(r['points'][-1]['corrected_kw'], 0.)

    def test_latest_closed_quarter_required(self):
        self.assertEqual(correct_forecast(self.history[:-1], self.future, self.now)['status'], 'unavailable')

    def test_recent_overprediction_reserves_headroom_without_changing_estimate(self):
        for p in self.history: p['actual_kw'] = 100.
        self.history[-1]['actual_kw'] = 50.
        r = correct_forecast(self.history, self.future, self.now)
        self.assertEqual(r['status'], 'unchanged')
        self.assertEqual(r['points'][0]['corrected_kw'], 100.)
        self.assertEqual(r['points'][0]['solver_kw'], 70.)
        self.assertEqual(r['points'][8]['solver_kw'], 100.)
        self.assertEqual(r['recent_overprediction_kw'], 50.)

    def test_missing_latest_measurement_cannot_create_reserve(self):
        for p in self.history: p['actual_kw'] = 0.
        r = correct_forecast(self.history[:-1], self.future, self.now)
        self.assertEqual(r['points'][0]['solver_kw'], 100.)

    def test_read_aggregates_minutes_and_keeps_source_identity(self):
        client = Mock()
        client.list_rows.return_value = [dict(timestamp=(self.now-timedelta(minutes=i)).isoformat(),
            es_sn='ES02', ac_solar_power='50') for i in range(1, 61)]
        inputs = dict(sources=dict(pv=dict(generated_at=(self.now-timedelta(hours=3)).isoformat(),
            forecast_start=(self.now-timedelta(hours=2)).isoformat(), version='pv-original', run_id='pv-run')),
            request=dict(points=[dict(timestamp=p['timestamp'].isoformat(), pv_forecast_kw=p['forecast_kw'])
                for p in self.history+self.future]))
        r = read_correction(client, 'station-2', inputs, self.now)
        self.assertEqual(r['ratio'], -.3)
        self.assertEqual(r['points'][0]['solver_kw'], 70.)
        self.assertEqual(r['points'][0]['reserve_candidate_kw'], 50.)
        self.assertFalse(r['reserve_applied'])
        self.assertEqual(r['source_version'], 'pv-original')
        self.assertTrue(all(p['valid_minutes'] == 15 for p in r['observations']))
        client.list_rows.return_value.append(client.list_rows.return_value[0])
        with self.assertRaises(ValueError): read_correction(client, 'station-2', inputs, self.now)

    def test_replay_switches_batch_without_reusing_old_residuals(self):
        from m4.scripts.replay_pv_correction import replay
        def report(start, name, forecast):
            return dict(run_id=name, forecast_start=start.isoformat(),
                forecast_end=(start+timedelta(days=1)).isoformat(), pairs=[dict(
                    target_time=(start+timedelta(minutes=15*i)).isoformat(),
                    forecast_kw=forecast, actual_kw=50., valid_minutes=15) for i in range(96)])
        result = replay([report(self.now-timedelta(hours=4), 'old', 100.),
                         report(self.now, 'new', 200.)])
        current = [r for r in result['rows'] if r['as_of'] == self.now.isoformat()]
        self.assertTrue(current)
        self.assertTrue(all(r['run_id'] == 'new' and r['original_kw'] == 200.
                            and r['status'] == 'unavailable' for r in current))
