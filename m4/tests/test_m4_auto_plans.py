import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from m4.settings.auto_plans import AutomaticPlans

class AutomaticPlanTests(unittest.TestCase):
    def test_once_per_window_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            service=Mock();service.running=set();service.start.return_value={'status':'running'}
            auto=AutomaticPlans(service,Path(d))
            auto.tick(1800);auto.tick(1801)
            self.assertEqual(service.start.call_count,2)
            AutomaticPlans(service,Path(d)).tick(1802)
            self.assertEqual(service.start.call_count,2)
            auto.tick(2700)
            self.assertEqual(service.start.call_count,4)

    def test_busy_station_retried_without_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            service=Mock();service.running={'station-1'}
            auto=AutomaticPlans(service,Path(d));auto.tick(1800)
            service.start.assert_called_once_with('station-2')
            service.running.clear();auto.tick(1810)
            self.assertEqual(service.start.call_count,2)

    def test_one_station_failure_does_not_stop_other(self):
        with tempfile.TemporaryDirectory() as d:
            service=Mock();service.running=set();service.start.side_effect=[ValueError('blocked'),{}]
            auto=AutomaticPlans(service,Path(d))
            with self.assertLogs("m4.settings.auto_plans",level="ERROR"):
                auto.tick(1800)
            self.assertEqual(service.start.call_count,2)
            auto.tick(1810);self.assertEqual(service.start.call_count,2)

    def test_shared_lock_and_disabled_mode(self):
        import fcntl
        with tempfile.TemporaryDirectory() as d:
            service=Mock();service.running=set()
            root=Path(d)
            with (root/'.automatic.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                AutomaticPlans(service,root).tick(1800)
                service.start.assert_not_called()
            AutomaticPlans(service,root,enabled=False).tick(1800)
            service.start.assert_not_called()
            AutomaticPlans(service,root).tick(1800)
            self.assertEqual(service.start.call_count,2)

    def test_lifespan_starts_and_stops_automatic_service(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from m4.settings.api import create_app
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'M4_AUTO_PLAN_ENABLED':'0'}):
            app=create_app(Path(d)/'settings.sqlite3')
            automatic=app.state.automatic_plans
            with patch.object(automatic,'start') as start,patch.object(automatic,'stop') as stop:
                with TestClient(app) as client:
                    start.assert_called_once()
                    self.assertFalse(automatic.metadata()['enabled'])
                stop.assert_called_once()
