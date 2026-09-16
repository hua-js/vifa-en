"""Legacy rolling tables must not override or fill the current M3 task."""
import copy
import unittest
from datetime import timedelta

from m4.settings.forecast_source import load_forecast
from m4.tests.test_m4_forecast_source import Client as ManualClient, DAY, START, run


def rolling(begin=START-timedelta(minutes=15), *, station='ES01', status='ok'):
    return dict(station_id=station, as_of=(begin+timedelta(minutes=2)).isoformat(),
        generated_at=(begin+timedelta(minutes=3)).isoformat(), source_data_end=begin.isoformat(),
        status=status, content_hash='a'*64, series_payload=[dict(unique_id='station_total_load',
        unit='kW', model_name='SeasonalNaive', status=status,
        points=[dict(data_time=(begin+timedelta(minutes=15*i)).isoformat(),
            target_time=(begin+timedelta(minutes=15*(i+1))).isoformat(), horizon_step=i+1,
            raw_forecast=1000.0+i, forecast_value=1000.0+i, is_clipped=False) for i in range(96)])])


class Client(ManualClient):
    def __init__(self, latest=None, runs=None):
        super().__init__(runs or [])
        self.latest = [rolling()] if latest is None else latest

    def list_rows(self, table, **kwargs):
        if table == 'energy_forecast_latest':
            self.calls.append((table, kwargs))
            return copy.deepcopy(self.latest)
        return super().list_rows(table, **kwargs)


class RollingForecastTests(unittest.TestCase):
    def test_legacy_rolling_values_cannot_replace_current_task(self):
        client=Client(runs=[run(1,DAY)])
        result=load_forecast(client,'station-1',plan_start_at=DAY,now=START)
        self.assertEqual(result['values'],list(range(100,196)))
        self.assertEqual(result['source_kind'],'current_task')
        self.assertEqual(client.calls,[('current_load_result','ES01')])

    def test_legacy_and_next_day_runs_cannot_fill_current_task_gap(self):
        client=Client(runs=[run(1,DAY),run(2,DAY+timedelta(days=1))])
        result=load_forecast(client,'station-1',plan_start_at=START,now=START)
        self.assertEqual(result['coverage_points'],24)
        self.assertEqual(result['values'][24:],[None]*72)
        self.assertEqual(result['horizon_points'],96)
        self.assertTrue(result['issues'])
        self.assertEqual(client.calls,[('current_load_result','ES01')])

    def test_missing_current_task_cannot_fall_back_to_rolling(self):
        with self.assertRaises(ValueError):
            load_forecast(Client(),'station-1',plan_start_at=START,now=START)

    def test_invalid_current_task_cannot_fall_back_to_valid_rolling(self):
        item=run(1,DAY);item['status']='failed'
        with self.assertRaises(ValueError):
            load_forecast(Client(runs=[item]),'station-1',plan_start_at=START,now=START)

    def test_legacy_changes_do_not_change_current_forecast_identity(self):
        def fetch(latest):
            return load_forecast(Client(latest=latest,runs=[run(1,DAY)]),'station-1',plan_start_at=DAY,now=START)
        original=fetch([rolling()])
        self.assertEqual(original['version'],fetch([])['version'])
        self.assertEqual(original['version'],fetch([rolling(status='degraded')])['version'])
