import json
from pathlib import Path
from threading import Event, Thread
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from m4.settings.decision_runs import DecisionRunError, DecisionRunManager


class DecisionRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = DecisionRunManager(self.root, SimpleNamespace(request=lambda *args: None))

    def test_per_station_locks_idempotency_and_no_ai_gate(self):
        started, release = Event(), Event()
        threads = []
        def thread_factory(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread
        def blocked(api, output, **kwargs):
            started.set()
            release.wait(5)
            return {'status': 'blocked_configuration', 'finished_at': '2026-09-08T01:00:00+00:00'}
        first, other = str(uuid4()), str(uuid4())
        with patch('m4.settings.decision_runs.run_chain', side_effect=blocked), \
                patch('m4.settings.decision_runs.Thread', side_effect=thread_factory):
            try:
                job = self.manager.start('station-1', first)
                self.assertTrue(started.wait(2))
                self.assertEqual(self.manager.start('station-1', first)['run_id'], first)
                with self.assertRaises(DecisionRunError) as caught:
                    self.manager.start('station-1', other)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.manager.start('station-2', other)['station_id'], 'station-2')
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(job['status'], 'running')

    def test_interrupted_job_never_restarts_and_station_does_not_bleed(self):
        run_id = str(uuid4())
        directory = self.root / 'station-1' / '.web-jobs'
        directory.mkdir(parents=True)
        (directory / (run_id + '.json')).write_text(json.dumps(dict(run_id=run_id,
            station_id='station-1', status='running', stage='selection',
            started_at='2026-09-08T00:00:00+00:00', finished_at=None, message='working')))
        view = self.manager.latest('station-1')
        self.assertEqual(view['job']['status'], 'interrupted')
        self.assertNotIn('模型', view['job']['message'])
        self.assertIsNone(self.manager.latest('station-2')['job'])
        self.assertEqual(self.manager.start('station-1', run_id)['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
