from datetime import datetime,timedelta
from unittest import TestCase
from m4.settings.pv_forecast_source import validate_source, ForecastRefreshRequired
from m4.settings.timeseries import InputDataError


def at(value): return datetime.fromisoformat(value)

def source(day):
    begin=at(day+'T06:15:00+08:00')
    return dict(source_kind='operational_forecast',es_sn='ES02',
                as_of=(begin-timedelta(minutes=15)).isoformat(),
                generated_at=(begin-timedelta(minutes=14)).isoformat(),
                forecast_start=begin.isoformat(),forecast_end=(begin+timedelta(days=1)).isoformat())


class DailyValidityTests(TestCase):
    def check(self, value, now, start, end):
        return validate_source(value, now=at(now), start=at(start), end=at(end))

    def test_same_day_afternoon_does_not_expire_after_two_hours(self):
        self.check(source('2026-09-10'),'2026-09-10T18:00:00+08:00','2026-09-10T06:15:00+08:00','2026-09-11T00:00:00+08:00')

    def test_yesterday_batch_works_before_six_in_beijing_including_utc_clock(self):
        self.check(source('2026-09-09'),'2026-09-09T21:00:00+00:00','2026-09-10T00:00:00+08:00','2026-09-10T06:15:00+08:00')

    def test_failed_today_prediction_can_use_yesterday_overlap(self):
        self.check(source('2026-09-09'),'2026-09-10T12:00:00+08:00','2026-09-10T00:00:00+08:00','2026-09-10T06:15:00+08:00')

    def test_two_days_old_batch_is_rejected(self):
        with self.assertRaisesRegex(ForecastRefreshRequired,'今天或昨天'):
            self.check(source('2026-09-08'),'2026-09-10T05:00:00+08:00','2026-09-08T06:15:00+08:00','2026-09-09T06:15:00+08:00')

    def test_future_generated_batch_is_rejected(self):
        with self.assertRaises(InputDataError):
            self.check(source('2026-09-10'),'2026-09-10T05:59:00+08:00','2026-09-10T06:15:00+08:00','2026-09-11T00:00:00+08:00')

    def test_window_outside_batch_is_still_rejected_by_rolling_validator(self):
        with self.assertRaisesRegex(ForecastRefreshRequired,'未覆盖'):
            self.check(source('2026-09-10'),'2026-09-10T12:00:00+08:00','2026-09-11T06:15:00+08:00','2026-09-12T06:15:00+08:00')
