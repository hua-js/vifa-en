"""Natural-day PV gaps: only user-approved night intervals may be zero-filled."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from .timeseries import InputDataError

POLICY = 'pv-night-zero-20-07-v1'
ZONE = ZoneInfo('Asia/Shanghai')


def fill_night_gaps(values, start):
    if len(values) != 96:
        raise InputDataError('光伏预测时间轴不完整，请等待 M3 更新。')
    missing = [i for i, value in enumerate(values) if value is None]
    for i in missing:
        at = (start + timedelta(minutes=15*i)).astimezone(ZONE)
        if 7 <= at.hour < 20:
            raise InputDataError('白天光伏预测不完整，请等待 M3 更新后生成日计划。')
    return [0.0 if value is None else value for value in values], missing
