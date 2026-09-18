from dataclasses import replace
import unittest
from unittest.mock import patch
from m3.tests.test_m3_custom_forecasting import make_config, make_dataset, FORECAST_START
from m3.worker.domain.custom_forecasting import seasonal_naive_champion, select_custom_champion, forecast_custom_series

class SocScheduleRegressionTests(unittest.TestCase):
    def dataset(self):
        dataset=make_dataset('storage_soc')
        # Mon-Sat discharge during daytime; Sunday stays charged.
        dataset.frame['y']=[98.0 if t.weekday()==6 else (98.0 if t.hour<8 else max(10.,98.-11.*(t.hour-7))) for t in dataset.frame['ds']]
        return dataset

    def test_monday_does_not_repeat_charged_sunday(self):
        dataset=self.dataset()
        champion=select_custom_champion(dataset,make_config())
        result=forecast_custom_series(dataset,champion,make_config())
        self.assertLess(result.points[16].forecast_value,50.)
        self.assertLess(sum(p.forecast_value>=99 for p in result.points),3)

    def test_weekly_failure_falls_back_to_matching_workday(self):
        dataset=self.dataset()
        champion=replace(seasonal_naive_champion(dataset),model_name='SOCWeeklyDelta')
        with patch('m3.worker.domain.custom_forecasting._soc_weekly_delta_values',side_effect=ValueError('missing')):
            result=forecast_custom_series(dataset,champion,make_config())
        self.assertLess(result.points[16].forecast_value,50.)
        self.assertEqual(result.model_name,'SOCScheduleDelta')
        self.assertEqual(result.status,'degraded')

    def test_saturation_does_not_delay_following_discharge(self):
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        dataset=self.dataset()
        dataset.frame['y']=[40. if t.hour==0 else 90. if t.hour==1 else 50. if t.hour==2 else 98. for t in dataset.frame['ds']]
        values=_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=3)
        self.assertEqual(values,[98.,98.,58.])
        from m3.worker.domain.custom_forecasting import _soc_weekly_delta_values
        self.assertEqual(_soc_weekly_delta_values(dataset,origin=FORECAST_START,periods=3),[98.,148.,59.])

    def test_no_matching_history_fails_instead_of_copying_sunday(self):
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        from m3.worker.errors import M3Error
        dataset=self.dataset()
        dataset=replace(dataset,frame=dataset.frame.loc[dataset.frame['ds'].dt.weekday==6])
        with self.assertRaises(M3Error):
            _soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=24)

    def test_future_and_imputed_soc_are_not_donors(self):
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        import pandas as pd
        dataset=self.dataset()
        expected=_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=24)
        frame=dataset.frame.copy()
        last=frame['ds'].iloc[-1].to_pydatetime()
        frame['y']=frame['y'].astype(object)
        frame.loc[frame.index[-1],'y']=object()
        dataset=replace(dataset,imputed_keys=frozenset({('storage_soc',last)}),frame=pd.concat([frame,pd.DataFrame([{'unique_id':'storage_soc','ds':FORECAST_START,'y':object()}])]))
        self.assertEqual(_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=24),expected)

    def test_short_horizon_scores_only_target_weekday(self):
        from m3.tests.test_m3_custom_forecasting import soc_dataset_with_weekly_pattern,make_selection_config
        dataset=soc_dataset_with_weekly_pattern()
        champion=select_custom_champion(dataset,make_selection_config(28,forecast_days=1))
        self.assertTrue(all(score.scorable_point_count==24 for score in champion.candidate_scores))

    def test_recent_plateau_prevents_recharging_high_monday_to_old_peak(self):
        from datetime import timedelta
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        dataset=self.dataset()
        # Older working days charged toward 98, but the last Sunday plateau is 83.
        dataset.frame['y']=[83. if t.weekday()==6 else min(98.,5.+t.hour*15.) for t in dataset.frame['ds']]
        values=_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=8)
        self.assertLessEqual(max(values),83.)

    def test_monday_reference_does_not_use_tuesday_discharge(self):
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        dataset=self.dataset()
        dataset.frame['y']=[80. if t.weekday()==6 else (80.-t.hour if t.weekday()==0 else max(2.,80.-4*t.hour)) for t in dataset.frame['ds']]
        values=_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=8)
        self.assertEqual(values[-1],73.)

    def test_rest_day_plateau_is_not_carried_into_ordinary_tuesday(self):
        from datetime import timedelta
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        dataset=self.dataset()
        dataset.frame['ds']=dataset.frame['ds']+timedelta(days=1)
        dataset.frame['y']=[83. if t.weekday() in (0,6) else min(98.,5.+t.hour*15.) for t in dataset.frame['ds']]
        values=_soc_schedule_delta_values(dataset,origin=FORECAST_START+timedelta(days=1),periods=3)
        self.assertGreater(values[1],83.)

    def test_matching_initial_soc_can_prefer_an_older_monday(self):
        from datetime import timedelta
        import pandas as pd
        from m3.worker.domain.custom_forecasting import _soc_schedule_delta_values
        dataset=self.dataset()
        times=pd.date_range(FORECAST_START-timedelta(days=14),periods=14*24,freq='1h')
        values=[]
        for t in times:
            if t.weekday()==0:
                values.append(80.-t.hour if t.date()==times[0].date() else max(2.,20.-t.hour*4.))
            else:
                values.append(80.)
        dataset=replace(dataset,frame=pd.DataFrame({'unique_id':'storage_soc','ds':times,'y':values}))
        result=_soc_schedule_delta_values(dataset,origin=FORECAST_START,periods=8)
        self.assertEqual(result[-1],73.)
