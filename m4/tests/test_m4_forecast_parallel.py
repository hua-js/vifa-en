"""Verify concurrency with handshakes, not wall-clock timing thresholds."""
import threading
from unittest import TestCase
from unittest.mock import patch
from m4.settings import forecast_source as source
from m4.tests.test_m4_forecast_source import Client, DAY, run


class ParallelForecastTests(TestCase):
    def test_gate_is_in_flight_while_forecasts_are_read(self):
        started=threading.Event(); release=threading.Event()
        original=source._load_rolling_forecast
        def gate(*args):
            started.set()
            if not release.wait(2): raise AssertionError('forecast read did not overlap')
            return {'version':'gate/v1','issues':[],'status':'ready'}
        def rolling(*args,**kwargs):
            try:self.assertTrue(started.wait(1),'MAPE request starts too late')
            finally:release.set()
            return original(*args,**kwargs)
        with patch.object(source,'read_gate',side_effect=gate),patch.object(source,'_load_rolling_forecast',side_effect=rolling):
            result=source.load_forecast(Client([run(1,DAY)]),'station-1',plan_start_at=DAY,now=DAY,require_full_day=True)
        self.assertEqual(result['coverage_points'],96)
        self.assertEqual(result['accuracy_gate']['version'],'gate/v1')

    def test_invalid_station_never_starts_upstream_reads(self):
        with patch.object(source,'read_gate') as gate:
            with self.assertRaises(ValueError):source.load_forecast(Client([]),'invalid',plan_start_at=DAY,now=DAY)
        gate.assert_not_called()

    def test_missing_mape_still_blocks_with_same_forecast_values(self):
        with patch.object(source,'read_gate',return_value={'version':'gate/blocked','issues':['MAPE unavailable'],'status':'unavailable'}):
            result=source.load_forecast(Client([run(1,DAY)]),'station-1',plan_start_at=DAY,now=DAY,require_full_day=True)
        self.assertEqual(result['coverage_points'],96)
        self.assertEqual(result['issues'],['MAPE unavailable'])

    def test_forecast_failure_joins_the_accuracy_worker(self):
        started=threading.Event(); release=threading.Event(); finished=threading.Event()
        def gate(*args):
            started.set()
            try:release.wait(2);return {'version':'gate/v1','issues':[]}
            finally:finished.set()
        def fail(*args,**kwargs):
            self.assertTrue(started.wait(1));release.set();raise ValueError('invalid forecast')
        with patch.object(source,'read_gate',side_effect=gate),patch.object(source,'_load_rolling_forecast',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'invalid forecast'):
                source.load_forecast(Client([]),'station-1',plan_start_at=DAY,now=DAY)
        self.assertTrue(finished.is_set())
