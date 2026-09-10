import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def module():
    assert importlib.util.find_spec('m3_worker.services.pv_hourly') is not None,'hourly state service missing'
    from m3_worker.services import pv_hourly
    return pv_hourly


class HourlyTests(unittest.TestCase):
    def test_successful_hour_is_skipped_but_next_hour_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            def operation(path,state):
                calls.append(path)
                return {'status':'completed','run_id':'one','archive_snapshot':'archive','training_source':'source'}
            first=module().execute_hour(root,'2026-09-09T22:05:00+08:00',operation)
            second=module().execute_hour(root,'2026-09-09T22:59:00+08:00',operation)
            third=module().execute_hour(root,'2026-09-09T23:05:00+08:00',operation)
            self.assertEqual(first['status'],'completed')
            self.assertEqual(second['status'],'already_completed')
            self.assertEqual(third['status'],'completed')
            self.assertEqual(len(calls),2)

    def test_failure_does_not_advance_success_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def fail(*args): raise ValueError('expected stage failure')
            with self.assertRaises(ValueError):module().execute_hour(root,'2026-09-09T22:05:00+08:00',fail)
            self.assertFalse((root/'state.json').exists())
            result=module().execute_hour(root,'2026-09-09T22:06:00+08:00',lambda *a:{'status':'completed'})
            self.assertEqual(result['status'],'completed')

    def test_concurrent_execution_is_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def operation(*args):
                with self.assertRaises(BlockingIOError):
                    module().execute_hour(root,'2026-09-09T22:05:00+08:00',lambda *a:self.fail('concurrent work'))
                return {'status':'completed'}
            module().execute_hour(root,'2026-09-09T22:05:00+08:00',operation)

    def test_incomplete_result_never_counts_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(ValueError):
                module().execute_hour(root,'2026-09-09T22:05:00+08:00',lambda *a:{'status':'running'})
            self.assertFalse((root/'state.json').exists())


if __name__=='__main__':unittest.main()
