from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

from m3.worker.custom_forecast_contracts import CustomObservationPoint
from m3.worker.errors import M3Error
from m3.worker.services.rolling_soc_service import RollingSocService, rolling_soc


ORIGIN = datetime.fromisoformat('2026-09-21T12:00:00+08:00')


def point(at, power=600.):
    return CustomObservationPoint(unique_id='storage_soc', ds=at, y=90.,
        quality='valid', source_state='valid', source_revision=0, storage_power_kw=power)


class RollingSocTests(TestCase):
    def forecast(self, points, origin=ORIGIN):
        with patch('m3.worker.services.rolling_soc_service._historical_delta', return_value=-1.), \
             patch('m3.worker.services.rolling_soc_service.calibrated_power_deltas',
                   side_effect=lambda frame, real, **kw: {t: -5. for t in real}):
            return rolling_soc(points, 3132., origin)

    def test_stable_power_updates_near_term_and_preserves_actual_anchor(self):
        result = self.forecast([point(ORIGIN - timedelta(minutes=n)) for n in (30, 15)])
        self.assertTrue(result['recent_power_correction'])
        self.assertEqual(result['points'][0]['forecast_value'], 90.)
        self.assertEqual(result['points'][1]['forecast_value'], 85.)
        # After the correction hour, increments return to the historical profile.
        values = [p['forecast_value'] for p in result['points']]
        self.assertEqual(values[5] - values[4], -1.)
        self.assertEqual(result['points'][-1]['target_time'], '2026-09-22T00:00:00+08:00')

    def test_single_spike_and_future_observation_do_not_drive_projection(self):
        result = self.forecast([point(ORIGIN - timedelta(minutes=30), 100.),
                               point(ORIGIN - timedelta(minutes=15)), point(ORIGIN, -9999.)])
        self.assertFalse(result['recent_power_correction'])
        self.assertEqual(result['points'][1]['forecast_value'], 89.)

    def test_missing_latest_bucket_fails(self):
        with self.assertRaises(M3Error):
            self.forecast([point(ORIGIN - timedelta(minutes=30))])

    def test_first_increment_uses_origin_bucket_not_next_quarter(self):
        previous = ORIGIN - timedelta(days=3)  # Friday shares Monday's day class.
        source_times = [previous + timedelta(minutes=15 * i) for i in range(48)]
        deltas = {t: 0. for t in source_times}
        deltas[source_times[0]] = -5.
        deltas[source_times[1]] = 5.
        points = [point(t) for t in source_times] + [point(ORIGIN - timedelta(minutes=15))]
        with patch('m3.worker.services.rolling_soc_service.calibrated_power_deltas', return_value=deltas), \
             patch('m3.worker.services.rolling_soc_service.schedule_day', return_value=True), \
             patch('m3.worker.services.rolling_soc_service.schedule_slot', return_value=(True, 0)):
            result = rolling_soc(points, 3132., ORIGIN)
        self.assertEqual([p['forecast_value'] for p in result['points'][:3]], [90., 85., 90.])

    def test_scheduler_continues_other_stages_when_rolling_fails(self):
        from m3.worker.scheduler.runner import SchedulerRunner
        rolling = Mock()
        rolling.update.side_effect = M3Error('source_stale', 'no current SOC')
        forecast = Mock()
        runner = SchedulerRunner(['ES02'], forecast, Mock(), acceptance_enabled=False,
                                 rolling_soc_service=rolling)
        at = ORIGIN.replace(minute=2)
        runner.tick(at)
        rolling.update.assert_called_once_with('ES02', at)
        forecast.run_forecast.assert_called_once_with('ES02', at)

    def test_midnight_and_last_quarter_horizons(self):
        for origin, length in ((ORIGIN.replace(hour=0), 97), (ORIGIN.replace(hour=23, minute=45), 2)):
            result = self.forecast([point(origin - timedelta(minutes=n)) for n in (30, 15)], origin)
            self.assertEqual(len(result['points']), length)

    def test_snapshot_expiry_and_failure_never_return_old_curve(self):
        source = Mock()
        service = RollingSocService(source, ['ES02'])
        self.assertEqual(service.snapshot('ES02', ORIGIN)['status'], 'pending')
        service._snapshots['ES02'] = {'station_id': 'ES02', 'status': 'ready',
            'origin': ORIGIN.isoformat(), 'points': [{'forecast_value': 90.}]}
        self.assertEqual(service.snapshot('ES02', ORIGIN + timedelta(minutes=31))['points'], [])
        source.list_custom_observations.side_effect = M3Error('source_http_failed', 'unavailable')
        with self.assertRaises(M3Error):
            service.update('ES02', ORIGIN)
        self.assertEqual(service.snapshot('ES02', ORIGIN)['status'], 'unavailable')
        self.assertEqual(service.snapshot('ES02', ORIGIN)['points'], [])
