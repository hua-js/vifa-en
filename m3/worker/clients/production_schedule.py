"""Read-only production calendar and confirmed NocoBase overtime adapter."""
from datetime import date, timedelta
import json
import math
from zoneinfo import ZoneInfo

import httpx

from m3.worker.clients.http import RetryPolicy, send_with_retry
from m3.worker.domain.production_schedule import PERIODS, ProductionSchedule, PRODUCTION_SCHEDULE_POLICY
from m3.worker.errors import M3Error

SHANGHAI = ZoneInfo("Asia/Shanghai")
API_URL = "https://yun.vifa.cn/api/production/schedule"
MAX_BYTES = 4 * 1024 * 1024


def parse_date(value):
    if type(value) is not str:
        raise ValueError("invalid date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid date")
    return parsed


def days_between(start, end):
    return (start + timedelta(days=i) for i in range((end - start).days + 1))


class ProductionScheduleClient:
    def __init__(self, api_key, client, overtime_api, stations, now, retry=None):
        if not api_key or not api_key.strip():
            raise ValueError("production schedule API key is required")
        self._key = api_key
        self._client = client
        self._api = overtime_api
        self._stations = {s.station_id: s.station_key for s in stations}
        self._now = now
        self._retry = retry or RetryPolicy()

    def _read(self, start, end, department):
        request = self._client.build_request("GET", API_URL,
            headers={"X-Api-Key": self._key}, params={
                "startDate": start.isoformat(), "endDate": end.isoformat(), "deptCodes": department,
            })
        def consume(response):
            if response.status_code != 200:
                raise M3Error("schedule_source_failed", "生产排班接口暂不可用")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise M3Error("schedule_source_invalid", "生产排班数据异常")
            try:
                envelope = json.loads(content)
                if type(envelope) is not dict or type(envelope.get("code")) is not int:
                    raise ValueError("invalid envelope")
                if envelope["code"] != 200:
                    raise M3Error("schedule_source_rejected", "生产排班接口拒绝请求")
                return envelope["data"]
            except (KeyError, TypeError, ValueError):
                raise M3Error("schedule_source_invalid", "生产排班数据异常") from None
        return send_with_retry(self._client, request, self._retry, stream=True, response_consumer=consume)

    def load(self, station_id, start: date, end: date, *, forecast_start: date):
        if station_id not in self._stations or start > end or (end - start).days > 365:
            raise M3Error("schedule_request_invalid", "排班查询参数无效")
        station_key = self._stations[station_id]
        department, station_sn = {"station_1": ("86", "ES01"), "station_2": ("113", "ES02")}[station_key]
        statuses, facts, horizons = {}, [], []
        try:
            cursor = start
            while cursor <= end:
                chunk_end = min(end, cursor + timedelta(days=185))
                data = self._read(cursor, chunk_end, department)
                if type(data) is not dict or data.get("startDate") != cursor.isoformat() or data.get("endDate") != chunk_end.isoformat():
                    raise ValueError("date echo mismatch")
                horizon = parse_date(data["scheduleHorizonEnd"])
                horizons.append(horizon.isoformat())
                rows = data["rows"]
                if type(rows) is not list or type(data.get("total")) is not int or len(rows) != data["total"]:
                    raise ValueError("invalid row count")
                for row in rows:
                    day = parse_date(row["date"])
                    status = row["status"]
                    if row["deptCode"] != department or not cursor <= day <= chunk_end or day > horizon or day in statuses or status not in {"WORK", "REST", "HOLIDAY", "SHUTDOWN"}:
                        raise ValueError("invalid calendar row")
                    fact = {"date": day.isoformat(), "status": status}
                    for field in ("scheduledHeadcount", "workHeadcount", "restHeadcount", "holidayHeadcount", "workRatio", "plannedHours"):
                        number = row[field]
                        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                            raise ValueError("invalid schedule metric")
                        fact[field] = number
                    statuses[day] = status
                    facts.append(fact)
                cursor = chunk_end + timedelta(days=1)
            rows = self._api.list_records_all("production_overtime", filter={
                "$and": [{"production_date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, {"status": "confirmed"}],
            }, fields=["id", "production_date", "stations", "period", "status", "createdAt", "updatedAt"], sort=["id"])
            overtime, records = {}, []
            seen_ids = set()
            for row in rows:
                stations = row["stations"]
                if type(stations) is not list or not stations or any(s not in {"ES01", "ES02"} for s in stations) or len(stations) != len(set(stations)):
                    raise ValueError("invalid overtime stations")
                if station_sn not in stations:
                    continue
                day = parse_date(row["production_date"])
                period = row["period"]
                if not start <= day <= end or row["status"] != "confirmed" or period not in PERIODS or row["id"] in seen_ids:
                    raise ValueError("invalid overtime record")
                if type(row["id"]) is not int or row["id"] < 1:
                    raise ValueError("invalid overtime identity")
                seen_ids.add(row["id"])
                previous = overtime.setdefault(day, [])
                if period in previous or "all_day" in previous or period == "all_day" and previous:
                    raise M3Error("schedule_overtime_conflict", "临时加班时段重复，请检查记录")
                previous.append(period)
                records.append({k: row[k] for k in ("id", "production_date", "period", "createdAt", "updatedAt")})
            # Future absence is not REST. A whole-day confirmed exception is sufficient.
            missing = [day for day in days_between(max(start, forecast_start), end)
                       if day not in statuses and "all_day" not in overtime.get(day, ())]
            if missing:
                raise M3Error("schedule_unavailable", "预测日期尚无生产排班")
            manifest = {
                "policy": PRODUCTION_SCHEDULE_POLICY, "station_id": station_id,
                "station_sn": station_sn, "department": department,
                "start": start.isoformat(), "end": end.isoformat(),
                "fetched_at": self._now().astimezone(SHANGHAI).isoformat(),
                "schedule_horizon_end": horizons, "daily_rows": facts, "overtime_records": records,
                "period_seconds": PERIODS,
                "historical_fallback_dates": [d.isoformat() for d in days_between(start, min(end, forecast_start - timedelta(days=1))) if d not in statuses],
                "historical_fallback_policy": "mon-sat-work-sun-rest-v1",
                "shift_policy": "08-12_14-18_19-21_station2-saturday-morning-v1",
                "historical_information_basis": "current_read_not_as_of_archive",
                "daily_api_is_shift_schedule": False,
            }
            return ProductionSchedule(station_id, station_key, department, start, end, forecast_start,
                statuses, {d: tuple(p) for d, p in overtime.items()}, json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        except M3Error:
            raise
        except (KeyError, ValueError, TypeError, OverflowError):
            raise M3Error("schedule_source_invalid", "生产排班或临时加班数据异常") from None
