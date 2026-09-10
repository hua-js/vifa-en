"""M4 consumes published PV predictions without starting a prediction job."""
from datetime import datetime, timedelta
from unittest import TestCase
from unittest.mock import Mock, patch

from m4.settings.daily_inputs import DailyInputService
from m4.settings.pv_forecast_source import ForecastRefreshRequired


class ReadOnlyPVTests(TestCase):
    def test_daily_station_two_reads_forecast_instead_of_history(self):
        live = Mock()
        service = DailyInputService(live)
        at = datetime.fromisoformat('2026-09-10T00:00:00+08:00')
        with patch('m4.settings.daily_inputs.load_pv_forecast', return_value={'values': [1]*96, 'forecast_start': at.isoformat(), 'forecast_end': (at+timedelta(days=1)).isoformat(), 'coverage_points': 96}) as read, patch('m4.settings.daily_inputs.validate_pv_source') as validate:
            result = service._pv('station-2', at, at)
        read.assert_called_once_with(live.client, plan_start_at=at, now=at)
        validate.assert_called_once()
        live._pv.assert_not_called()
        self.assertEqual(result['values'], [1]*96)

    def test_missing_forecast_never_falls_back_to_history(self):
        live = Mock(); service = DailyInputService(live)
        at = datetime.fromisoformat('2026-09-10T00:00:00+08:00')
        with patch('m4.settings.daily_inputs.load_pv_forecast', side_effect=ForecastRefreshRequired('等待M3')):
            with self.assertRaises(ForecastRefreshRequired): service._pv('station-2', at, at)
        live._pv.assert_not_called()

    def test_station_one_retains_no_pv_source(self):
        live = Mock(); service = DailyInputService(live)
        at = datetime.fromisoformat('2026-09-10T00:00:00+08:00')
        service._pv('station-1', at, at)
        live._pv.assert_called_once_with('station-1', at, at)

    def test_no_active_generation_client_remains(self):
        from m4.settings import pv_on_demand
        self.assertFalse(hasattr(pv_on_demand, 'PVService'))
        self.assertFalse(hasattr(pv_on_demand, 'PreparedInputs'))

    def test_valid_published_batch_and_window_gap(self):
        at = datetime.fromisoformat('2026-09-10T05:59:00+08:00')
        begin = at.replace(hour=6, minute=0)
        run = dict(id=1, run_id='pv-batch', es_sn='ES02', run_kind='operational',
                   status='completed', interval_minutes=15, expected_points=96,
                   model_name='WeatherRidge', model_version='v1', content_hash='a'*64,
                   weather_batch_id='b'*64, as_of=at.isoformat(),
                   generated_at=(at+timedelta(seconds=1)).isoformat(),
                   forecast_start=begin.isoformat(), forecast_end=(begin+timedelta(days=1)).isoformat())
        points = [dict(run_pk=1, horizon_step=i+1, forecast_kw=10,
                       target_time=(begin+timedelta(minutes=15*i)).isoformat()) for i in range(96)]
        live = Mock(); live.client.list_rows.side_effect = lambda table, **kw: [run] if table.endswith('runs') else points
        service = DailyInputService(live)
        result = service._pv('station-2', begin, begin)
        self.assertEqual(result['coverage_points'], 96)
        self.assertEqual(result['run_id'], 'pv-batch')
        filled = service._pv('station-2', begin.replace(hour=0), begin)
        self.assertEqual(filled['values'][:24], [0.0]*24)
        self.assertEqual(filled['values'][24:], [10.0]*72)
        self.assertEqual(filled['zero_filled_points'], 24)
        self.assertEqual(filled['forecast_coverage_points'], 72)
        self.assertEqual(filled['gap_policy'], 'outside_forecast_window_zero')
        self.assertEqual(service._pv('station-2', begin, begin+timedelta(hours=3))['coverage_points'], 96)
        with self.assertRaisesRegex(Exception, '没有重叠'):
            service._pv('station-2', begin.replace(hour=0)-timedelta(days=1), begin)
        points.pop()
        with self.assertRaisesRegex(Exception, '预测点不完整'):
            service._pv('station-2', begin.replace(hour=0), begin)
        live._pv.assert_not_called()
        self.assertTrue(all(c.args[0] in ('energy_pv_forecast_runs', 'energy_pv_forecast_points') for c in live.client.list_rows.call_args_list))
