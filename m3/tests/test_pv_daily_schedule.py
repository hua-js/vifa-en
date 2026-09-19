"""Daily PV schedule: Beijing time, durable deduplication and manual exclusion."""
from datetime import datetime
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from m3.worker.services.pv_manual_jobs import ManualJobs


class DailyScheduleTests(unittest.TestCase):
    def test_beijing_six_once_per_day_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            seen = []
            operation = lambda kind, path: (seen.append(kind) or {'status': 'completed'})
            jobs = ManualJobs(Path(temp), operation)
            self.assertFalse(jobs.submit_daily(datetime.fromisoformat('2026-09-09T21:59:59+00:00')))
            self.assertTrue(jobs.submit_daily(datetime.fromisoformat('2026-09-09T22:00:00+00:00')))
            jobs.close()
            jobs = ManualJobs(Path(temp), operation)
            self.assertFalse(jobs.submit_daily(datetime.fromisoformat('2026-09-10T06:00:45+08:00')))
            self.assertFalse(jobs.submit_daily(datetime.fromisoformat('2026-09-11T06:01:00+08:00')))
            self.assertTrue(jobs.submit_daily(datetime.fromisoformat('2026-09-11T06:00:00+08:00')))
            jobs.close()
            self.assertEqual(seen, ['forecast', 'forecast'])

    def test_busy_job_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            release = threading.Event()
            def operation(*args):
                release.wait(2)
                return {'status': 'completed'}
            jobs = ManualJobs(Path(temp), operation)
            try:
                _, original = jobs.submit('weather')
                self.assertFalse(jobs.submit_daily(datetime.fromisoformat('2026-09-10T06:00:00+08:00')))
                self.assertEqual(jobs.latest()['job_id'], original['job_id'])
                self.assertFalse((Path(temp)/'daily-schedule.json').exists())
            finally:
                release.set(); jobs.close()

    def test_failed_job_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            def fail(*args): raise ValueError('failure')
            jobs = ManualJobs(Path(temp), fail)
            at = datetime.fromisoformat('2026-09-10T06:00:00+08:00')
            self.assertTrue(jobs.submit_daily(at)); jobs.close()
            jobs = ManualJobs(Path(temp), fail)
            self.assertEqual(jobs.latest()['status'], 'failed')
            self.assertFalse(jobs.submit_daily(at)); jobs.close()

    def test_schedule_thread_stops_with_service(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = ManualJobs(Path(temp), lambda *a: {'status': 'completed'})
            checked = threading.Event()
            with patch.object(jobs, 'submit_daily', side_effect=lambda at: checked.set()):
                jobs.start_daily_schedule()
                original = jobs._schedule_thread
                jobs.start_daily_schedule()
                self.assertIs(jobs._schedule_thread, original)
                self.assertTrue(checked.wait(2))
                jobs.close()
                self.assertFalse(original.is_alive())

    def test_claim_survives_uncertain_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = ManualJobs(Path(temp), lambda *a: {'status': 'completed'})
            at = datetime.fromisoformat('2026-09-10T06:00:00+08:00')
            try:
                with patch.object(jobs, 'submit', side_effect=OSError('uncertain')):
                    with self.assertRaises(OSError): jobs.submit_daily(at)
                self.assertFalse(jobs.submit_daily(at))
                self.assertIsNone(jobs.latest())
            finally:
                jobs.close()

    def test_deployment_flag_controls_scheduler(self):
        from m3.worker.pv_service_app import build_manager
        with patch('m3.worker.pv_service_app.ManualJobs') as manager:
            with patch.dict('os.environ', {'PV_DAILY_SCHEDULE_ENABLED': '0'}):
                build_manager()
                manager.return_value.start_daily_schedule.assert_not_called()
            with patch.dict('os.environ', {'PV_DAILY_SCHEDULE_ENABLED': '1'}):
                build_manager()
                manager.return_value.start_daily_schedule.assert_called_once()

    def test_evening_and_morning_have_independent_durable_claims(self):
        with tempfile.TemporaryDirectory() as temp:
            seen = []
            operation = lambda *args: (seen.append(1) or {'status': 'completed'})
            for at, expected in (
                ('2026-09-10T06:00:00+08:00', True),
                ('2026-09-10T14:00:00+00:00', True),
                ('2026-09-10T22:00:30+08:00', False),
                ('2026-09-10T06:00:30+08:00', False),
                ('2026-09-11T06:00:00+08:00', True),
            ):
                jobs = ManualJobs(Path(temp), operation)
                try:
                    self.assertEqual(jobs.submit_daily(datetime.fromisoformat(at)), expected)
                finally:
                    jobs.close()
            self.assertEqual(len(seen), 3)

    def test_legacy_morning_claim_does_not_block_first_evening(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp)/'daily-schedule.json').write_text(json.dumps({
                'last_attempt_date': '2026-09-10', 'scheduled_time': '06:00', 'status': 'claimed'}))
            jobs = ManualJobs(Path(temp), lambda *a: {'status': 'completed'})
            try:
                self.assertFalse(jobs.submit_daily(datetime.fromisoformat('2026-09-10T06:00:00+08:00')))
                self.assertTrue(jobs.submit_daily(datetime.fromisoformat('2026-09-10T22:00:00+08:00')))
            finally:
                jobs.close()

    def test_uncertain_evening_is_not_replayed_but_next_morning_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = ManualJobs(Path(temp), lambda *a: {'status': 'completed'})
            at = datetime.fromisoformat('2026-09-10T22:00:00+08:00')
            try:
                with patch.object(jobs, 'submit', side_effect=OSError('uncertain')):
                    with self.assertRaises(OSError):
                        jobs.submit_daily(at)
            finally:
                jobs.close()
            jobs = ManualJobs(Path(temp), lambda *a: {'status': 'completed'})
            try:
                self.assertFalse(jobs.submit_daily(at))
                self.assertTrue(jobs.submit_daily(datetime.fromisoformat('2026-09-11T06:00:00+08:00')))
            finally:
                jobs.close()
