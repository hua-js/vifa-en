import unittest

import numpy as np
import pandas as pd

from m3_worker.domain.pv_training import prepare_pv, align_weather, INSTANT_FIELDS, RADIATION_FIELDS
from m3_worker.domain.pv_backtest import fit_weather_models, predict_weather, rolling_backtest, metric_values, summarize


TZ = 'Asia/Shanghai'


def minutes(count=60):
    return [dict(es_sn='ES02', timestamp=at.isoformat(), ac_solar_power=100)
            for at in pd.date_range('2026-08-01', periods=count, freq='min', tz=TZ)]


def weather():
    frame = pd.DataFrame(index=pd.date_range('2026-08-01', periods=2, freq='h', tz=TZ))
    for name in INSTANT_FIELDS:
        frame[name] = [20.0, 28.0]
    for name in RADIATION_FIELDS:
        frame[name] = [0.0, 400.0]
    frame['precipitation_mm'] = [0.0, 4.0]
    return frame


class PVTrainingTests(unittest.TestCase):
    def test_previous_hour_radiation_and_instant_midpoint_are_aligned_separately(self):
        result = align_weather(prepare_pv(minutes()), weather())
        self.assertEqual(result.ghi_wm2.tolist(), [400.0]*4)
        self.assertEqual(result.temperature_c.tolist(), [21.0, 23.0, 25.0, 27.0])
        self.assertEqual(result.precipitation_mm.tolist(), [1.0]*4)
        self.assertTrue(result.eligible.all())
        self.assertTrue((result.weather_radiation_time == pd.Timestamp('2026-08-01 01:00', tz=TZ)).all())

    def test_twelve_distinct_minutes_qualify_but_eleven_and_nulls_do_not(self):
        rows = minutes()
        for i in [0, 1, 2, 15, 16, 17, 18]:
            rows[i]['ac_solar_power'] = None
        rows.extend([dict(es_sn='ES02', timestamp='2026-08-01T00:19:30+08:00', ac_solar_power=100)])
        result = prepare_pv(rows)
        self.assertEqual(result.valid_minutes.iloc[:2].tolist(), [12, 11])
        self.assertEqual(result.actual_kw.iloc[0], 100)
        self.assertTrue(pd.isna(result.actual_kw.iloc[1]))
        self.assertFalse(result.hour_valid.any())

    def test_multiple_samples_in_one_minute_do_not_overweight_power(self):
        rows = minutes()
        rows[0]['ac_solar_power'] = 0
        rows.append(dict(es_sn='ES02', timestamp='2026-08-01T00:00:30+08:00', ac_solar_power=200))
        self.assertEqual(prepare_pv(rows).actual_kw.iloc[0], 100.0)

    def test_invalid_station_duplicate_time_and_invalid_power_are_rejected(self):
        for changed in [dict(es_sn='ES01'), dict(ac_solar_power=-1),
                        dict(ac_solar_power=float('nan')), dict(ac_solar_power=True),
                        dict(timestamp='2026-08-01T00:00:00')]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                rows = minutes()
                rows[0].update(changed)
                prepare_pv(rows)
        with self.assertRaises(ValueError):
            prepare_pv(minutes()+[minutes()[0]])

    def test_missing_hour_end_weather_excludes_points_without_filling(self):
        result = align_weather(prepare_pv(minutes()), weather().iloc[:1])
        self.assertFalse(result.eligible.any())
        self.assertTrue(result.ghi_wm2.isna().all())
        self.assertTrue((result.rejection_reason == 'weather_missing').all())


def aligned_days():
    index = pd.date_range('2026-08-01', periods=4*96, freq='15min', tz=TZ)
    frame = pd.DataFrame(index=index)
    frame['ghi_wm2'] = np.maximum(0, np.sin((index.hour + index.minute/60 - 6)*np.pi/12))*800
    frame.loc[(index.hour < 6) | (index.hour >= 18), 'ghi_wm2'] = 0
    frame['direct_horizontal_wm2'] = frame.ghi_wm2*.6
    frame['dhi_wm2'] = frame.ghi_wm2*.4
    frame['temperature_c'] = 28.0
    frame['relative_humidity_pct'] = 70.0
    frame['cloud_cover_pct'] = 35.0
    frame['wind_speed_kmh'] = 8.0
    frame['actual_kw'] = frame.ghi_wm2*.5
    frame['eligible'] = True
    return frame


class PVBacktestTests(unittest.TestCase):
    def test_ridge_matches_closed_form_for_three_collinear_scaled_features(self):
        frame = aligned_days().iloc[:2].copy()
        frame.index = pd.DatetimeIndex(['2026-08-01T00:00:00+08:00', '2026-08-02T00:00:00+08:00'])
        frame['ghi_wm2'] = [100.0, 200.0]
        frame['actual_kw'] = [50.0, 100.0]
        frame['temperature_c'] = 25.0
        for field in ('cloud_cover_pct', 'relative_humidity_pct', 'wind_speed_kmh', 'direct_horizontal_wm2', 'dhi_wm2'):
            frame[field] = 0.0
        prediction = predict_weather(frame, fit_weather_models(frame))['ridge_kw']
        # GHI, GHI*sin(clock), GHI*cos(clock) are the same RMS-scaled
        # vector: the nonzero eigenvalue is 3, so L2 shrinkage is 3/3.01.
        np.testing.assert_allclose(prediction, np.array([50.0, 100.0])*3/3.01, rtol=1e-10)

    def test_future_weather_does_not_change_training_scaling(self):
        frame = aligned_days()
        _, before = rolling_backtest(frame, min_complete_days=2)
        frame.loc[frame.index >= pd.Timestamp('2026-08-03', tz=TZ), 'ghi_wm2'] *= 100
        _, after = rolling_backtest(frame, min_complete_days=2)
        self.assertEqual(before[0]['model'], after[0]['model'])

    def test_test_day_power_does_not_change_that_days_model_or_predictions(self):
        frame = aligned_days()
        before, folds = rolling_backtest(frame, min_complete_days=2)
        frame.loc[frame.index >= pd.Timestamp('2026-08-03', tz=TZ), 'actual_kw'] *= 10
        after, changed_folds = rolling_backtest(frame, min_complete_days=2)
        day = before.index < pd.Timestamp('2026-08-04', tz=TZ)
        np.testing.assert_allclose(before.loc[day, ['ridge_kw', 'gain_kw']], after.loc[day, ['ridge_kw', 'gain_kw']])
        self.assertEqual(folds[0]['model'], changed_folds[0]['model'])
        self.assertLessEqual(pd.Timestamp(folds[0]['max_training_label_end']), pd.Timestamp('2026-08-03', tz=TZ))

    def test_irradiance_gain_is_learned_and_zero_weather_yields_zero_power(self):
        frame = aligned_days()
        model = fit_weather_models(frame.iloc[:96])
        prediction = predict_weather(frame.iloc[96:192], model)
        np.testing.assert_allclose(prediction['gain_kw'], frame.actual_kw.iloc[96:192], atol=1e-8)
        zero = frame.iloc[96:192].ghi_wm2.to_numpy() == 0
        self.assertTrue((prediction['ridge_kw'][zero] == 0).all())
        self.assertTrue(np.isfinite(prediction['ridge_kw']).all())
        self.assertTrue((prediction['ridge_kw'] >= 0).all())

    def test_missing_yesterday_slot_is_not_replaced_by_positional_shift(self):
        frame = aligned_days()
        missing = pd.Timestamp('2026-08-03 12:00', tz=TZ)
        frame.loc[missing, 'actual_kw'] = np.nan
        frame.loc[missing, 'eligible'] = False
        points, _ = rolling_backtest(frame, min_complete_days=2)
        self.assertTrue(pd.isna(points.loc[missing + pd.Timedelta(days=1), 'baseline_kw']))

    def test_metric_values_match_hand_calculation_and_zero_energy_is_not_divided(self):
        metrics = metric_values(np.array([100., 200.]), np.array([80., 220.]))
        self.assertEqual(metrics['mae_kw'], 20.0)
        self.assertEqual(metrics['rmse_kw'], 20.0)
        self.assertAlmostEqual(metrics['wape_pct'], 40/300*100)
        self.assertIsNone(metric_values(np.array([0.]), np.array([1.]))['wape_pct'])

    def test_partial_day_has_no_full_day_energy_and_metrics_share_same_points(self):
        points = aligned_days().iloc[:96].copy()
        for col in ('baseline_kw', 'gain_kw', 'ridge_kw'):
            points[col] = points.actual_kw
        points.loc[points.index[48], 'baseline_kw'] = np.nan
        result = summarize(points)
        self.assertEqual(result['shared_points'], 95)
        self.assertEqual(result['shared_full_days'], 0)
        self.assertIsNone(result['daily'][0]['actual_kwh'])
        for values in result['models'].values():
            self.assertEqual(values['all']['points'], 95)


if __name__ == '__main__':
    unittest.main()
