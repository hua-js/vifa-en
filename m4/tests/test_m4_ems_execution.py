"""Version-scoped EMS table acceptance must not leak between plans."""
from copy import deepcopy
import unittest
from datetime import datetime
from m4.settings.ems_execution import annotate_execution
from m4.settings.ems_remaining_plan import matches_body
from m4.settings.rolling_history import advisory_history


class EMSExecutionTests(unittest.TestCase):
    def setUp(self):
        self.point = dict(timestamp='2026-09-18T10:00:00+08:00', mode='discharge', target_power_kw=300)
        self.payload = dict(date='2026-09-18', effective_at=self.point['timestamp'], ems_table_write=dict(
            station_id='station-2', run_id='run-a', effective_at=self.point['timestamp'],
            status='plan_table_readback_verified', execution_basis='ems_plan_table_readback_v1',
            plan_date='2026-09-18', confirmed_plan=[deepcopy(self.point)]))

    def evidence(self, payload):
        points = [deepcopy(self.point)]
        annotate_execution('station-2', 'run-a', payload, points)
        return points[0]['ems_execution']

    def test_readback_acceptance_including_zero_network_operations(self):
        self.payload['ems_table_write']['network_write_performed'] = False
        self.assertEqual(self.evidence(self.payload), 'confirmed')

    def test_other_run_day_station_or_unmatched_point_never_confirms(self):
        for key, value in [('run_id', 'run-b'), ('station_id', 'station-1'),
                           ('plan_date', '2026-09-17'), ('confirmed_plan', []),
                           ('execution_basis', None)]:
            with self.subTest(key=key):
                p = deepcopy(self.payload)
                p['ems_table_write'][key] = value
                self.assertEqual(self.evidence(p), 'not_confirmed')
        p = deepcopy(self.payload)
        p['ems_table_write']['confirmed_plan'][0]['mode'] = 'charge'
        self.assertEqual(self.evidence(p), 'not_confirmed')

    def test_partial_failure_does_not_claim_execution(self):
        self.payload['ems_table_write'].update(status='table_write_unconfirmed', completed_operations=2)
        self.assertEqual(self.evidence(self.payload), 'unconfirmed')

    def test_date_readback_accepts_midnight_but_not_different_date(self):
        body = dict(m4_run_id='run-a', m4_plan_date='2026-09-18')
        self.assertTrue(matches_body(dict(body, m4_plan_date='2026-09-18T00:00:00.000Z'), body))
        self.assertFalse(matches_body(dict(body, m4_plan_date='2026-09-17'), body))
        self.assertFalse(matches_body(dict(body, m4_run_id='run-b'), body))

    def test_archived_history_retains_only_its_own_confirmation(self):
        p = deepcopy(self.payload)
        p.update(station_id='station-2', schema_version='m4-rolling-plan-v1',
                 dispatch_status='not_dispatched', finished_at='2026-09-18T09:59:00+08:00',
                 valid_until='2026-09-18T10:15:00+08:00', daily_run_id='daily-a', source='ems',
                 plan=[dict(self.point, expected_soc_pct=50, grid_import_kw=200)])
        job = dict(station_id='station-2', status='completed', run_id='run-a', result=p)
        now = datetime.fromisoformat('2026-09-18T10:16:00+08:00')
        h = advisory_history('station-2', [job], now)
        self.assertEqual(h['points'][0]['ems_execution'], 'confirmed')
        job['run_id'] = 'run-b'
        h = advisory_history('station-2', [job], now)
        self.assertEqual(h['points'][0]['ems_execution'], 'not_confirmed')
