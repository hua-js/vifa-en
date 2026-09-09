import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient

from m4_settings.api import create_app
from m4_settings.billing import BillingService, TZ, month_bounds
from m4_settings.upstream import SourceReadError

NOW = datetime(2026, 9, 2, 12, tzinfo=TZ)
DAY = dict(id=1, es_sn='ES01', createdAt='2026-08-31T16:00:04Z', updatedAt='2026-09-01T15:45:00Z',
           day_charge='100.5', day_discharge=90, charge_cost=30, discharge_earnings=20, day_earnings=-10)
MONTH = dict(id=2, es_sn='ES01', createdAt='2026-08-31T16:00:05Z', updatedAt='2026-09-02T00:00:00Z',
             month_charge=100, month_discharge=90, month_earnings=-10, month_costs=0, month_income=0, profit=0)


class Client:
    def __init__(self):
        self.rows = {'t_es_stat': [copy.deepcopy(DAY)], 't_es_count_stat': [copy.deepcopy(MONTH)]}
        self.calls = []

    def list_rows(self, table, **kwargs):
        self.calls.append((table, kwargs))
        rows = self.rows[table]
        if isinstance(rows, Exception):
            raise rows
        return rows


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.service = BillingService(self.client)

    def fetch(self, **kwargs):
        return self.service.fetch('station-1', '2026-09', now=NOW, **kwargs)

    def test_timezone_source_fields_and_independent_totals(self):
        result = self.fetch()
        self.assertEqual(result['daily'][0]['date'], '2026-09-01')
        self.assertEqual(result['daily_totals']['day_earnings'], -10)
        self.assertEqual(result['monthly']['month_costs'], 0)
        self.assertEqual(result['differences']['month_charge'], -.5)
        self.assertEqual(result['coverage']['missing_dates'], ['2026-09-02'])
        self.assertEqual(result['period_status'], 'ongoing')
        self.assertEqual(result['settlement_status'], 'unverified')
        for table, query in self.client.calls:
            self.assertEqual(query['filters']['$and'][0], {'es_sn': {'$eq': 'ES01'}})
            self.assertEqual(query['filters']['$and'][1]['createdAt'], {'$gte': '2026-09-01T00:00:00+08:00', '$lt': '2026-10-01T00:00:00+08:00'})
            self.assertNotIn('total_earnings', query['fields'])

    def test_missing_values_are_not_zero_or_partial_sum(self):
        self.client.rows['t_es_stat'][0]['charge_cost'] = None
        result = self.fetch()
        self.assertIsNone(result['daily_totals']['charge_cost'])
        self.assertEqual(result['daily_totals']['discharge_earnings'], 20)
        self.assertTrue(any('字段缺失' in text for text in result['issues']))

    def test_empty_and_both_failed_sources(self):
        self.client.rows = dict(t_es_stat=[], t_es_count_stat=[])
        result = self.fetch()
        self.assertEqual(result['status'], 'empty')
        self.assertIsNone(result['monthly'])
        self.assertIsNone(result['daily_totals']['day_charge'])
        self.client.rows = {table: SourceReadError('读取失败') for table in self.client.rows}
        self.assertEqual(self.fetch()['status'], 'error')

    def test_one_failed_source_preserves_the_other_without_fallback(self):
        self.client.rows['t_es_count_stat'] = SourceReadError('月统计接口读取失败')
        result = self.fetch()
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['monthly'])
        self.assertEqual(len(result['daily']), 1)
        self.assertIsNone(result['differences']['month_charge'])

    def test_duplicate_periods_do_not_double_count(self):
        for table in self.client.rows:
            with self.subTest(table=table):
                original = copy.deepcopy(self.client.rows[table])
                self.client.rows[table] *= 2
                result = self.fetch()
                self.assertEqual(result['sources']['daily' if table == 't_es_stat' else 'monthly']['status'], 'error')
                self.client.rows[table] = original

    def test_wrong_station_month_naive_time_and_bad_numbers_are_rejected(self):
        for key, value in [('es_sn', 'ES02'), ('createdAt', '2026-09-30T16:00:00Z'),
                           ('createdAt', '2026-09-01T00:00:00'), ('day_charge', -1),
                           ('day_charge', True), ('day_earnings', 'NaN'), ('day_earnings', 'Infinity')]:
            with self.subTest(key=key, value=value):
                self.client.rows['t_es_stat'] = [{**DAY, key: value}]
                result = self.fetch()
                self.assertEqual(result['sources']['daily']['status'], 'error')
                self.assertEqual(result['daily'], [])

    def test_historical_month_is_not_claimed_settled(self):
        result = self.service.fetch('station-1', '2026-09', now=datetime(2026, 10, 1, tzinfo=TZ))
        self.assertEqual(result['period_status'], 'historical')
        self.assertEqual(result['coverage']['expected_days'], 30)
        self.assertEqual(result['settlement_status'], 'unverified')

    def test_month_boundaries_leap_year_and_invalid_input(self):
        start, end = month_bounds('2024-02')
        self.assertEqual((end-start).days, 29)
        self.assertEqual(month_bounds('2026-12')[1].isoformat(), '2027-01-01T00:00:00+08:00')
        for value in ['2026-00', '2026-13', '2026-9', '2026-09&x=y', '1999-01', None]:
            with self.assertRaises(ValueError):
                month_bounds(value)

    def test_api_month_validation_station_scope_and_get_only(self):
        calls = []
        service = SimpleNamespace(fetch=lambda station, month: calls.append((station, month)) or {'station_id': station, 'month': month})
        with tempfile.TemporaryDirectory() as temp, TestClient(create_app(Path(temp)/'settings.db', billing_service=service)) as api:
            response = api.get('/m4-api/stations/station-2/bills?month=2026-09')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(calls, [('station-2', '2026-09')])
            self.assertEqual(api.get('/m4-api/stations/station-1/bills').status_code, 422)
            self.assertEqual(api.get('/m4-api/stations/station-1/bills?month=2026-13').status_code, 422)
            self.assertEqual(api.get('/m4-api/stations/other/bills?month=2026-09').status_code, 404)
            self.assertEqual(api.post('/m4-api/stations/station-1/bills?month=2026-09').status_code, 405)
            self.assertEqual(len(calls), 1)

    def test_api_unexpected_errors_are_sanitized(self):
        def fail(*args):
            raise RuntimeError('secret and URL')
        with tempfile.TemporaryDirectory() as temp, TestClient(create_app(Path(temp)/'settings.db', billing_service=SimpleNamespace(fetch=fail))) as api:
            response = api.get('/m4-api/stations/station-1/bills?month=2026-09')
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('secret', response.text)


if __name__ == '__main__':
    unittest.main()
