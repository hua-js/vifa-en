import copy
import importlib.util
import unittest

import numpy as np
import pandas as pd

from m3.worker.domain import pv_backtest, pv_training


def core():
    assert importlib.util.find_spec('m3.worker.domain.pv_operational') is not None, 'operational core missing'
    from m3.worker.domain import pv_operational
    return pv_operational


def weather_rows():
    start = pd.Timestamp('2026-09-09T21:00:00+08:00')
    return [dict(es_sn='ES02', source_kind='forecast', provider='open_meteo',
        source_batch_id='a'*64, interval_minutes=60, timezone='Asia/Shanghai',
        latitude='23.000000', longitude='113.000000', quality_status='valid',
        weather_time=(start+pd.Timedelta(hours=i)).isoformat(),
        fetched_at='2026-09-09T22:01:00+08:00', issued_at=None,
        **{f: float(i+20) for f in pv_training.WEATHER_FIELDS}) for i in range(50)]


def training():
    index = pd.date_range('2026-08-20', periods=8*96, freq='15min', tz=pv_training.TZ)
    frame = pd.DataFrame({f: 20.0 for f in pv_training.WEATHER_FIELDS}, index=index)
    frame['ghi_wm2'] = 500.0
    frame['actual_kw'] = 100.0
    return frame


class OperationalTests(unittest.TestCase):
    def test_strict_next_quarter_and_hour_end_weather_without_fake_power(self):
        frame = core().prepare_future(weather_rows(), '2026-09-09T22:15:00+08:00')
        self.assertEqual(len(frame), 96)
        self.assertEqual(frame.index[0].isoformat(), '2026-09-09T22:30:00+08:00')
        self.assertEqual(frame.index[-1].isoformat(), '2026-09-10T22:15:00+08:00')
        self.assertEqual(frame.iloc[0].ghi_wm2, 22.0)
        self.assertEqual(frame.iloc[0].temperature_c, 21.625)
        self.assertEqual(frame.iloc[0].precipitation_mm, 5.5)
        self.assertNotIn('actual_kw', frame.columns)

    def test_unavailable_mixed_or_incomplete_weather_rejected(self):
        for issue in ('future_fetch', 'future_issue', 'mixed_batch', 'wrong_station', 'gap', 'null', 'duplicate', 'old_window'):
            rows = weather_rows()
            at = '2026-09-09T22:15:00+08:00'
            if issue == 'future_fetch': rows[0]['fetched_at'] = '2026-09-10T00:00:00+08:00'
            if issue == 'future_issue': rows[0]['issued_at'] = '2026-09-10T00:00:00+08:00'
            if issue == 'mixed_batch': rows[2]['source_batch_id'] = 'b'*64
            if issue == 'wrong_station': rows[2]['es_sn'] = 'ES01'
            if issue == 'gap': rows.pop(4)
            if issue == 'null': rows[4]['ghi_wm2'] = None
            if issue == 'duplicate': rows[4] = copy.deepcopy(rows[3])
            if issue == 'old_window': at = '2026-09-11T22:00:00+08:00'
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                core().prepare_future(rows, at)

    def test_train_cutoff_excludes_future_labels_and_rms_uses_training_only(self):
        train = training()
        later = train.iloc[:1].copy()
        later.index = pd.DatetimeIndex(['2026-09-09T22:15:00+08:00']).tz_convert(pv_training.TZ)
        later['actual_kw'] = 99999999.0
        later['ghi_wm2'] = 99999999.0
        result = core().generate(pd.concat([train, later]), weather_rows(), '2026-09-09T22:15:00+08:00')
        expected = pv_backtest.fit_weather_models(train)
        self.assertEqual(result['model'], expected)
        self.assertEqual(len(result['training']), 768)
        self.assertEqual(len(result['points']), 96)
        self.assertTrue(np.isfinite(result['points'].forecast_kw).all())
        self.assertTrue((result['points'].forecast_kw >= 0).all())

    def test_insufficient_or_duplicate_training_rejected(self):
        for data in (training().iloc[:6*96], pd.concat([training(), training().iloc[:1]])):
            with self.assertRaises(ValueError):
                core().generate(data, weather_rows(), '2026-09-09T22:15:00+08:00')

    def test_raw_negative_power_is_preserved_and_night_zero(self):
        model = pv_backtest.fit_weather_models(training())
        model['weights'] = [-100.0] + [0.0]*8
        self.assertTrue(callable(getattr(pv_backtest, 'predict_raw_ridge', None)), 'raw ridge output missing')
        raw = pv_backtest.predict_raw_ridge(training().iloc[:1], model)
        self.assertEqual(raw[0], -100.0)
        self.assertEqual(pv_backtest.predict_weather(training().iloc[:1], model)['ridge_kw'][0], 0)
        night = training().iloc[:1].copy()
        night.loc[:, pv_training.RADIATION_FIELDS] = 0
        self.assertEqual(pv_backtest.predict_raw_ridge(night, model)[0], 0)


if __name__ == '__main__':
    unittest.main()
