from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

import pandas as pd

from m3.tests.test_m3_custom_forecasting import FORECAST_START, soc_dataset_with_weekly_pattern
from m3.worker.domain.custom_forecasting import _soc_power_values
from m3.worker.domain.soc_power import calibrated_power_deltas
from m3.worker.errors import M3Error


class SocPowerTests(unittest.TestCase):
    def history(self):
        dataset = soc_dataset_with_weekly_pattern()
        # 100 kWh; one-hour AC charge at 10 kW gives +9 points;
        # discharge at 8.1 kW gives -9 points (both efficiencies 0.9).
        values = [20. + 9. * min(t.hour, 24 - t.hour, 8)
                  for t in dataset.frame['ds']]
        dataset.frame['y'] = values
        dataset.frame['storage_power_kw'] = [
            -10. if 1 <= t.hour <= 8 else 8.1 if t.hour >= 17 or t.hour == 0 else 0.
            for t in dataset.frame['ds']]
        return replace(dataset, energy_capacity_kwh=100.)

    def test_power_to_soc_respects_charge_discharge_sign_and_efficiency(self):
        dataset = self.history()
        real = dict(zip(dataset.frame.ds, dataset.frame.y))
        deltas = calibrated_power_deltas(dataset.frame, real,
                                        interval_seconds=3600, capacity_kwh=100.)
        day = dataset.frame.ds.iloc[24]
        self.assertAlmostEqual(deltas[day + timedelta(hours=2)], 9.)
        self.assertAlmostEqual(deltas[day + timedelta(hours=18)], -9.)
        self.assertEqual(deltas[day + timedelta(hours=12)], 0.)

    def test_no_calibration_fails_instead_of_guessing_efficiency(self):
        dataset = self.history()
        dataset.frame['y'] = 99.
        with self.assertRaises(M3Error):
            calibrated_power_deltas(dataset.frame, dict(zip(dataset.frame.ds, dataset.frame.y)),
                                    interval_seconds=3600, capacity_kwh=100.)

    def test_future_and_imputed_power_do_not_change_prediction(self):
        dataset = self.history()
        with patch('m3.worker.domain.custom_forecasting.schedule_day', return_value=True), \
             patch('m3.worker.domain.custom_forecasting.schedule_slot', return_value=(True, 0)):
            # Remove a single historical point from the real donor/calibration set.
            timestamp = dataset.frame.ds.iloc[10].to_pydatetime()
            dataset = replace(dataset, imputed_keys=frozenset({('storage_soc', timestamp)}))
            expected = _soc_power_values(dataset, origin=FORECAST_START, periods=24)
            frame = dataset.frame.copy()
            frame.loc[frame.ds == timestamp, 'storage_power_kw'] = 1e9
            frame = pd.concat([frame, pd.DataFrame([{
                'unique_id': 'storage_soc', 'ds': FORECAST_START,
                'y': 99., 'storage_power_kw': -1e9,
            }])], ignore_index=True)
            actual = _soc_power_values(replace(dataset, frame=frame), origin=FORECAST_START, periods=24)
        self.assertEqual(actual, expected)

    def test_missing_power_is_not_idle(self):
        dataset = self.history()
        dataset.frame['storage_power_kw'] = None
        with self.assertRaises(M3Error):
            _soc_power_values(dataset, origin=FORECAST_START, periods=24)
