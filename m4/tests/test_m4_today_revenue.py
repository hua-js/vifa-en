import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Thread
from unittest.mock import patch

from m4.settings.today_revenue import TodayRevenueService, TZ


DAY = datetime(2026, 10, 20, tzinfo=TZ)


class Source:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def list_rows(self, table, **kwargs):
        self.calls.append((table, kwargs))
        bounds = kwargs['filters']['$and'][1]['timestamp']
        start, end = (datetime.fromisoformat(bounds[key]) for key in ('$gte', '$lte'))
        return [row for row in self.rows if start <= datetime.fromisoformat(row['timestamp']) <= end]


def rows(*powers):
    return [dict(timestamp=(DAY + timedelta(seconds=30*i)).isoformat(), es_sn='ES02',
                 grid_power=grid, load_power=load, ac_solar_power=pv, emus_power=storage)
            for i, (grid, load, pv, storage) in enumerate(powers)]


class TodayRevenueTests(unittest.TestCase):
    def service(self, records):
        service = TodayRevenueService(Source(records))
        service._tariff = lambda *args: {'values': [1.0]*96, 'sell_price_per_kwh': 0.3}
        return service

    def fetch(self, service, seconds=30):
        return service.fetch('station-2', now=DAY+timedelta(seconds=seconds))

    def test_import_with_pv_and_storage_reconciles_total(self):
        data = self.fetch(self.service(rows((30, 100, 50, 20), (30, 100, 50, 20))))
        cards = data['cards']
        self.assertEqual(cards['grid']['amount_yuan'], .25)
        self.assertEqual(cards['pv']['amount_yuan'], .42)
        self.assertEqual(cards['storage']['amount_yuan'], .17)
        self.assertEqual(cards['total']['amount_yuan'], .59)
        self.assertEqual(cards['total']['status'], 'ready')

    def test_pv_charging_storage_is_not_double_counted(self):
        cards = self.fetch(self.service(rows((-30, 20, 100, -50), (-30, 20, 100, -50))))['cards']
        self.assertEqual(cards['pv']['amount_yuan'], .37)
        self.assertEqual(cards['storage']['amount_yuan'], -.12)
        self.assertEqual(cards['total']['amount_yuan'], .25)
        self.assertEqual(cards['pv']['self_use_kwh'], .58)

    def test_bad_load_preserves_independent_measurements(self):
        cards = self.fetch(self.service(rows((10, -1, 20, -30), (10, -1, 20, -30))))['cards']
        self.assertIsNone(cards['storage']['amount_yuan'])
        self.assertIsNone(cards['pv']['amount_yuan'])
        self.assertIsNone(cards['total']['amount_yuan'])
        self.assertEqual(cards['storage']['charge_kwh'], .25)
        self.assertEqual(cards['pv']['generation_kwh'], .17)
        self.assertEqual(cards['storage']['status'], 'partial')
        self.assertEqual(cards['grid']['status'], 'ready')

    def test_bad_sample_excludes_both_adjacent_money_intervals_only(self):
        records = rows((30, 100, 50, 20), (30, -1, 50, 20),
                       (30, 100, 50, 20), (30, 100, 50, 20))
        data = self.fetch(self.service(records), 90)
        cards = data['cards']
        self.assertEqual(cards['storage']['amount_yuan'], .17)
        self.assertEqual(cards['pv']['amount_yuan'], .42)
        self.assertEqual(cards['total']['amount_yuan'], .59)
        self.assertEqual(cards['total']['amount_status'], 'estimated')
        self.assertEqual(cards['storage']['amount_coverage'], {'included_seconds': 30.0, 'excluded_seconds': 60.0})
        self.assertEqual(cards['storage']['status'], 'partial')
        self.assertEqual(cards['grid']['amount_yuan'], .75)
        self.assertEqual(cards['grid']['amount_status'], 'ready')
        self.assertEqual(cards['storage']['discharge_kwh'], .5)
        self.assertEqual(cards['pv']['generation_kwh'], 1.25)
        exclusions = data['coverage']['excluded_intervals']
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]['start_at'], DAY.isoformat())
        self.assertEqual(exclusions[0]['end_at'], (DAY+timedelta(seconds=60)).isoformat())
        self.assertEqual(exclusions[0]['reasons'], ['negative_load'])
        self.assertEqual(exclusions[0]['affected_cards'], ['storage', 'pv', 'total'])
        self.assertEqual(records[1]['load_power'], -1)

    def test_all_bad_money_intervals_remain_unavailable(self):
        data = self.fetch(self.service(rows((10, -1, 20, -30), (10, -1, 20, -30))))
        for name in ('storage', 'pv', 'total'):
            self.assertIsNone(data['cards'][name]['amount_yuan'])
            self.assertEqual(data['cards'][name]['amount_status'], 'unavailable')
        self.assertEqual(data['cards']['grid']['amount_status'], 'ready')

    def test_missing_grid_keeps_money_from_other_complete_intervals(self):
        records = rows((None, 100, 50, 20), (30, 100, 50, 20), (30, 100, 50, 20))
        cards = self.fetch(self.service(records), 60)['cards']
        self.assertEqual(cards['grid']['amount_yuan'], .25)
        self.assertEqual(cards['grid']['amount_status'], 'estimated')
        self.assertIsNone(cards['grid']['import_kwh'])
        self.assertEqual(cards['total']['amount_yuan'], .59)
        self.assertEqual(cards['total']['amount_status'], 'estimated')

    def test_missing_grid_preserves_generation_and_storage(self):
        cards = self.fetch(self.service(rows((None, 100, 20, 10), (None, 100, 20, 10))))['cards']
        self.assertEqual(cards['pv']['generation_kwh'], .17)
        self.assertIsNone(cards['pv']['self_use_kwh'])
        self.assertEqual(cards['storage']['discharge_kwh'], .08)
        self.assertIsNone(cards['grid']['import_kwh'])

    def test_no_tariff_preserves_energy(self):
        service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
        service._tariff = lambda *args: None
        cards = self.fetch(service)['cards']
        self.assertIsNone(cards['grid']['amount_yuan'])
        self.assertEqual(cards['grid']['import_kwh'], .25)
        self.assertEqual(cards['storage']['discharge_kwh'], .17)

    def test_quarter_boundary_uses_each_period_price(self):
        records = rows((120, 120, 0, 0), (120, 120, 0, 0))
        for index, record in enumerate(records):
            record['timestamp'] = (DAY+timedelta(minutes=14, seconds=45+30*index)).isoformat()
        service = self.service(records)
        service._tariff = lambda *args: {'values': [1.0]+[2.0]*95, 'sell_price_per_kwh': .3}
        self.assertEqual(self.fetch(service, 915)['cards']['grid']['amount_yuan'], 1.5)

    def test_power_crossing_splits_import_and_export(self):
        service = self.service(rows((120, 0, 0, -120), (-120, 0, 0, 120)))
        service._tariff = lambda *args: {'values': [1.0]*96, 'sell_price_per_kwh': .2}
        cards = self.fetch(service)['cards']
        self.assertEqual(cards['grid']['import_kwh'], .25)
        self.assertEqual(cards['grid']['export_kwh'], .25)
        self.assertEqual(cards['grid']['amount_yuan'], .2)
        self.assertEqual(cards['storage']['amount_yuan'], -.2)

    def test_gap_is_not_integrated_or_extrapolated(self):
        records = rows(*[(30, 100, 50, 20)]*4)
        records[2]['timestamp'] = (DAY+timedelta(seconds=120)).isoformat()
        records[3]['timestamp'] = (DAY+timedelta(seconds=150)).isoformat()
        data = self.fetch(self.service(records), 180)
        self.assertEqual(data['coverage']['covered_seconds'], 60)
        self.assertFalse(data['coverage']['complete'])
        self.assertEqual(data['cards']['total']['amount_yuan'], 1.16)
        self.assertEqual(data['cards']['total']['amount_status'], 'estimated')
        self.assertEqual(data['cards']['total']['amount_coverage']['excluded_seconds'], 90)

    def test_missing_day_start_keeps_later_revenue_as_estimate(self):
        records = rows((30, 100, 50, 20), (30, 100, 50, 20))
        for record in records:
            record['timestamp'] = (datetime.fromisoformat(record['timestamp'])+timedelta(seconds=30)).isoformat()
        data = self.fetch(self.service(records), 60)
        self.assertEqual(data['cards']['total']['amount_yuan'], .59)
        self.assertEqual(data['cards']['total']['amount_status'], 'estimated')
        self.assertEqual(data['coverage']['excluded_intervals'][0]['reasons'], ['missing_day_start'])

    def test_only_today_then_overlap_then_next_day_full_read(self):
        service = self.service(rows(*[(30, 100, 50, 20)]*9))
        from shared.project import get_project
        station = get_project().station('station-2')
        service._read(station, DAY+timedelta(seconds=180))
        service._read(station, DAY+timedelta(seconds=240))
        filters = [kwargs['filters'] for _, kwargs in service.client.calls]
        self.assertEqual(filters[0]['$and'][0], {'es_sn': {'$eq': 'ES02'}})
        self.assertEqual(filters[0]['$and'][1]['timestamp']['$gte'], DAY.isoformat())
        self.assertEqual(filters[1]['$and'][1]['timestamp']['$gte'], (DAY+timedelta(seconds=60)).isoformat())
        data = service.fetch('station-2', now=DAY+timedelta(days=1, seconds=30))
        self.assertEqual(data['coverage']['samples'], 0)
        self.assertIsNone(data['cards']['total']['amount_yuan'])

    def test_storage_log_splits_direction_and_groups_adjacent_samples(self):
        service = self.service(rows((0, 0, 0, -20), (0, 0, 0, -20), (0, 0, 0, 20)))
        data = service.storage_log('station-2', now=DAY+timedelta(seconds=60))
        discharge, charge = data['items']
        self.assertEqual(discharge['mode'], 'discharge')
        self.assertEqual(discharge['power_kw'], 10)
        self.assertEqual(charge['mode'], 'charge')
        self.assertEqual(charge['power_kw'], 16.667)
        self.assertEqual(charge['end_at'], discharge['start_at'])

    def test_source_failure_does_not_become_zero_revenue(self):
        service = self.service([])
        with patch.object(service.client, 'list_rows', side_effect=RuntimeError('unavailable')):
            data = self.fetch(service)
        self.assertEqual(data['sources']['measurements']['status'], 'error')
        self.assertIsNone(data['cards']['total']['amount_yuan'])
        self.assertIsNone(data['cards']['storage']['charge_kwh'])

    def test_fresh_results_share_one_read_and_cannot_mutate_the_cache(self):
        service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
        with patch('m4.settings.today_revenue.monotonic', return_value=0):
            first = self.fetch(service)
            first['cards']['total']['amount_yuan'] = 999
            second = self.fetch(service, 31)
        self.assertEqual(len(service.client.calls), 1)
        self.assertEqual(second['cards']['total']['amount_yuan'], .59)
        self.assertEqual(second['fetched_at'], (DAY+timedelta(seconds=30)).isoformat())
        self.assertEqual(second['refresh_status'], 'ready')

    def test_slow_refresh_returns_previous_result_without_duplicate_reads(self):
        service = self.service(rows(*[(30, 100, 50, 20)]*3))
        entered, release = Event(), Event()
        calculate = service._calculate
        threads = []

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release the source')
            return calculate(*args, **kwargs)

        def start_thread(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch('m4.settings.today_revenue.monotonic', return_value=0):
            first = self.fetch(service)
        try:
            with patch('m4.settings.today_revenue.monotonic', return_value=10), \
                    patch.object(service, '_calculate', side_effect=slow) as refresh, \
                    patch('m4.settings.today_revenue.Thread', side_effect=start_thread):
                second = self.fetch(service, 60)
                self.assertTrue(entered.wait(2))
                third = self.fetch(service, 60)
                self.assertEqual(second['cards'], first['cards'])
                self.assertEqual(third['refresh_status'], 'refreshing')
                self.assertEqual(second['fetched_at'], first['fetched_at'])
                self.assertEqual(refresh.call_count, 1)
                release.set()
                for thread in threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                updated = self.fetch(service, 60)
                self.assertEqual(updated['refresh_status'], 'ready')
                self.assertEqual(updated['coverage']['samples'], 3)
        finally:
            release.set()
            for thread in threads:
                thread.join(3)

    def test_cold_concurrent_requests_share_one_calculation(self):
        service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
        calculate = service._calculate
        entered, release, second_entered = Event(), Event(), Event()

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release the source')
            return calculate(*args, **kwargs)

        def second_read():
            second_entered.set()
            return self.fetch(service)

        with patch.object(service, '_calculate', side_effect=slow) as refresh, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.fetch, service)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(second_read)
                self.assertTrue(second_entered.wait(2))
            finally:
                release.set()
            self.assertEqual(first.result()['cards'], second.result()['cards'])
            self.assertEqual(refresh.call_count, 1)

    def test_failed_refresh_retains_original_timestamp_and_stops_reporting_ready(self):
        service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
        with patch('m4.settings.today_revenue.monotonic', return_value=0):
            original = self.fetch(service)
        with patch('m4.settings.today_revenue.monotonic', return_value=10), \
                patch.object(service.client, 'list_rows', side_effect=RuntimeError('offline')):
            # Complete one failed refresh synchronously to inspect its outcome.
            with service._result_locks['station-2']:
                service._refresh_result('station-2', DAY+timedelta(seconds=40))
            retained = self.fetch(service, 40)
        self.assertEqual(retained['cards'], original['cards'])
        self.assertEqual(retained['fetched_at'], original['fetched_at'])
        self.assertEqual(retained['refresh_status'], 'error')

    def test_cold_failure_is_shared_by_waiters_and_then_retried(self):
        service = self.service([])
        entered, release, second_entered = Event(), Event(), Event()

        def offline(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release the source')
            raise RuntimeError('offline')

        def second_read():
            second_entered.set()
            return self.fetch(service)

        with patch('m4.settings.today_revenue.monotonic', return_value=0), \
                patch.object(service.client, 'list_rows', side_effect=offline) as read, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.fetch, service)
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(second_read)
                self.assertTrue(second_entered.wait(2))
            finally:
                release.set()
            self.assertEqual(first.result()['refresh_status'], 'error')
            self.assertEqual(second.result()['refresh_status'], 'error')
            self.assertEqual(read.call_count, 1)
        with patch('m4.settings.today_revenue.monotonic', return_value=6), \
                patch.object(service.client, 'list_rows', return_value=[]) as retry:
            self.assertEqual(self.fetch(service, 36)['refresh_status'], 'ready')
            self.assertEqual(retry.call_count, 1)

    def test_expired_or_cross_day_result_is_not_used_after_source_failure(self):
        for seconds in (151, 86430):
            with self.subTest(seconds=seconds):
                service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
                self.fetch(service)
                with patch.object(service.client, 'list_rows', side_effect=RuntimeError('offline')):
                    data = self.fetch(service, seconds)
                self.assertIsNone(data['cards']['total']['amount_yuan'])
                self.assertEqual(data['refresh_status'], 'error')

    def test_results_are_isolated_by_station(self):
        service = self.service(rows((30, 100, 50, 20), (30, 100, 50, 20)))
        self.fetch(service)
        with patch.object(service.client, 'list_rows', return_value=[]):
            other = service.fetch('station-1', now=DAY+timedelta(seconds=30))
        self.assertEqual(other['station_id'], 'station-1')
        self.assertIsNone(other['cards']['total']['amount_yuan'])
