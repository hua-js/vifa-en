import copy
import importlib.util
import unittest

from m3.tests.test_pv_operational import training, weather_rows


def service():
    assert importlib.util.find_spec('m3.worker.services.pv_publication') is not None, 'publication service missing'
    from m3.worker.services import pv_publication
    return pv_publication


def bundle():
    return service().build_bundle(training(), weather_rows(), '2026-09-09T22:15:00+08:00',
        '2026-09-09T22:15:01+08:00', {'observed_at': '2026-09-09T22:14:00+08:00',
        'historical_weather_batch_id': 'b'*64, 'files': {}}, '2026-09-09T20:00:00+08:00')


class MemoryRepository:
    """Stateful API stand-in; assertions inspect stored business data."""
    def __init__(self):
        self.run = None
        self.points = []
        self.drop_last = False

    def read_run(self, run_id):
        return copy.deepcopy(self.run)

    def create_run(self, run):
        self.run = {**copy.deepcopy(run), 'id': 19}

    def read_points(self, run_pk):
        return copy.deepcopy(self.points)

    def create_points(self, points):
        self.points.extend(copy.deepcopy(points[:-1] if self.drop_last else points))

    def complete_run(self, run_pk):
        self.run['status'] = 'completed'


class PublicationTests(unittest.TestCase):
    def test_valid_bundle_roundtrips_96_points_and_repeat_has_zero_inserts(self):
        run, points = bundle()
        repo = MemoryRepository()
        report = service().publish(repo, run, points, now=lambda: '2026-09-09T22:16:00+08:00')
        self.assertEqual(repo.run['status'], 'completed')
        self.assertEqual(len(repo.points), 96)
        self.assertEqual(report['inserted_points'], 96)
        self.assertTrue(all(p['run_pk'] == 19 for p in repo.points))
        again = service().publish(repo, run, points, now=lambda: '2026-09-10T23:00:00+08:00')
        self.assertEqual(again['inserted_points'], 0)

    def test_partial_points_cannot_complete_and_resume_inserts_only_missing(self):
        run, points = bundle()
        repo = MemoryRepository()
        repo.drop_last = True
        with self.assertRaises(ValueError):
            service().publish(repo, run, points, now=lambda: '2026-09-09T22:16:00+08:00')
        self.assertEqual(repo.run['status'], 'running')
        self.assertEqual(len(repo.points), 95)
        repo.drop_last = False
        report = service().publish(repo, run, points, now=lambda: '2026-09-09T22:17:00+08:00')
        self.assertEqual(report['inserted_points'], 1)
        self.assertEqual(repo.run['status'], 'completed')

    def test_conflicting_stored_value_is_rejected_without_rewriting(self):
        run, points = bundle()
        repo = MemoryRepository()
        repo.create_run({**run, 'status': 'running'})
        repo.create_points([{**points[0], 'run_pk': 19, 'forecast_kw': '999.000000'}])
        with self.assertRaises(ValueError):
            service().publish(repo, run, points, now=lambda: '2026-09-09T22:16:00+08:00')
        self.assertEqual(repo.points[0]['forecast_kw'], '999.000000')
        self.assertEqual(repo.run['status'], 'running')

    def test_hash_duplicates_missing_point_and_forecast_tamper_rejected(self):
        run, points = bundle()
        for problem in ('hash', 'duplicate', 'missing', 'tamper'):
            r, p = copy.deepcopy(run), copy.deepcopy(points)
            if problem == 'hash': r['content_hash'] = '0'*64
            if problem == 'duplicate': p[1] = copy.deepcopy(p[0])
            if problem == 'missing': p.pop()
            if problem == 'tamper':
                p[0]['forecast_kw'] = '999.000000'
                r['content_hash'] = service().content_hash(r, p)
            with self.subTest(problem=problem), self.assertRaises(ValueError):
                service().validate_bundle(r, p)

    def test_stale_unpublished_window_is_rejected_and_verify_only_never_writes(self):
        run, points = bundle()
        repo = MemoryRepository()
        with self.assertRaises(ValueError):
            service().publish(repo, run, points, now=lambda: '2026-09-09T22:30:00+08:00')
        self.assertIsNone(repo.run)
        with self.assertRaises(ValueError):
            service().publish(repo, run, points, verify_only=True, now=lambda: '2026-09-09T22:16:00+08:00')
        self.assertIsNone(repo.run)

    def test_api_utc_dates_and_decimal_values_normalize_without_losing_precision(self):
        run, points = bundle()
        row = {**points[0], 'run_pk': '19', 'target_time': '2026-09-09T14:30:00.000Z'}
        row['forecast_kw'] = float(row['forecast_kw'])
        matched = service().compare_points([row], points, 19)
        self.assertEqual(matched, {1})
        row['forecast_kw'] = '1.0000001'
        with self.assertRaises(ValueError): service().compare_points([row], points, 19)


if __name__ == '__main__':
    unittest.main()
