"""Daily planning ignores score gates and retains the schedule-table write path."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from unittest.mock import Mock, patch

from m4.settings.daily_schedule import DailyScheduleService
from m4.settings.ems_model_update import ModelUpdateError, ZONE
from m4.settings.forecast_source import load_forecast
from m4.tests.test_m4_load_accuracy import NOW, row


class ForecastAdmissionTests(unittest.TestCase):
    def test_complete_low_quality_inputs_generate_and_reload_a_day_plan(self):
        from m4.settings.daily_inputs import DailyInputService
        from m4.settings.daily_plans import DailyPlanService
        from m4.settings.daily_pv_policy import POLICY as PV_POLICY
        from m4.settings.timeseries import PV_EXPORT_POLICY
        from m4.tests.test_m4_realtime import configuration
        now = datetime.now(ZONE)
        run_id = str(uuid4())
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        config = configuration('station-2', soc_min_pct=1., soc_max_pct=98.,
            preferred_soc_min_pct=2., preferred_soc_max_pct=98.,
            charge_efficiency=1., discharge_efficiency=1.)
        live = Mock()
        live.client.current_load_result.return_value = row('80', now=now)
        live._controls.return_value = dict(status='ready', station_id='station-2', version='ctrl',
            power_scope='station', source_health={'schedule': 'ready'}, schedule=[],
            demand={'need_kw': 1000.}, storage_capacity=dict(energy_capacity_kwh=3132.,
                scope='station', source_station_id='ES02', source_table='t_es', source_field='es_power_storage'))
        live._tariff.return_value = dict(values=[.2]*32+[1.]*64, period_types=['gu']*32+['feng']*64,
            version='tariff', export_price_policy=PV_EXPORT_POLICY, sell_price_per_kwh=.5)
        inputs = DailyInputService(live, score_optional=True)
        pv = dict(values=[0.]*96, version='pv', gap_policy=PV_POLICY)
        initial = dict(status='ready', scope='whole_station_reference', version='soc',
            observed_at=start.isoformat(), initial_soc_pct=50.)
        with TemporaryDirectory() as root, patch.object(inputs, '_pv', return_value=pv), \
                patch.object(inputs, '_initial_soc', return_value=initial), \
                patch.object(inputs, '_current_soc', return_value={}), \
                patch.object(inputs, '_current_power', return_value={}):
            bundle = inputs.fetch(config, now.date(), now=now)
            self.assertTrue(bundle['can_compare'], bundle['checks'])
            self.assertEqual(bundle['request']['source_versions']['load_admission_policy'], 'complete-forecast-v1')
            from m4.settings.daily_policy import matches_current_daily_policy
            self.assertTrue(matches_current_daily_policy('station-2', bundle['request']))
            old_preference = deepcopy(bundle['request'])
            old_preference['source_versions'].pop('initial_idle_preference')
            self.assertFalse(matches_current_daily_policy('station-2', old_preference))
            store = Mock()
            store.get.return_value = config
            service = DailyPlanService(store, inputs, root)
            service.jobs['station-2'] = {'started_at': now.isoformat()}
            service._run('station-2', run_id)
            output = service.jobs['station-2']
            self.assertEqual(output['status'], 'completed', output.get('message'))
            self.assertEqual(len(output['request']['points']), 96)
            comparison = output['result']['record']['daily_comparison']
            self.assertIsNotNone(comparison['recommended'])
            self.assertEqual(comparison['minimum_net_savings_yuan'], 100)
            self.assertEqual(DailyPlanService(store, inputs, root).latest('station-2')['run_id'], run_id)
            # The same generated plan must pass the dispatch contract and reach the writer.
            from m4.settings.daily_dispatch import daily_payload, DailyDispatchContext
            from m4.settings.daily_schedule import CONFIRMED
            dispatch_time = start + timedelta(hours=12, minutes=1)
            payload = daily_payload(output, config, now=dispatch_time)
            DailyDispatchContext()._context('station-2', payload, config, now=dispatch_time)
            writer = Mock()
            writer.status.return_value = {'enabled': True, 'status': 'waiting'}
            writer.submit.side_effect = lambda station, body, configuration: dict(
                status=CONFIRMED, confirmed_plan=body['plan'], confirmed_schedule=[])
            coordinator = DailyScheduleService(service, writer)
            with patch('m4.settings.daily_schedule.datetime', wraps=datetime) as clock, \
                    patch('m4.settings.daily_dispatch.datetime', wraps=datetime) as dispatch_clock:
                clock.now.return_value = dispatch_time
                dispatch_clock.now.return_value = dispatch_time
                coordinator._run('station-2', None)
                state = coordinator.latest('station-2')
                self.assertEqual(state['confirmed_daily_run_id'], run_id, state)
                self.assertEqual([call.args[1]['kind'] for call in writer.submit.call_args_list], ['expire', 'daily'])
                self.assertEqual(writer.submit.call_args.args[1]['plan'], payload['plan'])
                coordinator._run('station-2', None)
                self.assertEqual(writer.submit.call_count, 2, 'confirmed plans must stay frozen')
            # A stricter score policy must regenerate rather than silently reuse this result.
            inputs.score_optional = False
            self.assertEqual(service.latest('station-2')['status'], 'empty')

    def forecast(self, evidence, *, require_accuracy=False):
        client = Mock()
        client.current_load_result.return_value = evidence
        return load_forecast(client, 'station-2',
            plan_start_at=NOW.astimezone(ZONE).replace(hour=0, minute=0), now=NOW,
            require_full_day=True, require_accuracy=require_accuracy)

    def test_bad_or_absent_score_does_not_block_complete_forecast(self):
        for score in ('high', 'missing', 'no_actuals'):
            evidence = row('80')
            if score == 'missing':
                evidence.pop('current_score')
            elif score == 'no_actuals':
                for point in evidence['points']:
                    point.update(actual_value=None, actual_quality=None)
                evidence['current_score'].update(actual_count=0, valid_count=0, mape_percent=None)
            with self.subTest(score=score):
                source = self.forecast(evidence)
                self.assertEqual(source['issues'], [])
                self.assertEqual(source['coverage_points'], 96)
                self.assertNotEqual(source['accuracy_gate']['status'], 'ready')
                self.assertTrue(self.forecast(evidence, require_accuracy=True)['issues'])

    def test_missing_corrupt_wrong_station_and_stale_forecasts_remain_blocked(self):
        for change in ('missing', 'negative', 'station', 'duplicate', 'stale'):
            evidence = row('80')
            if change == 'missing':
                evidence['points'].pop()
            elif change == 'negative':
                evidence['points'][0]['forecast_value'] = -1
            elif change == 'station':
                evidence['run']['station_id'] = 'ES01'
            elif change == 'duplicate':
                evidence['points'][1] = deepcopy(evidence['points'][0])
            else:
                evidence = row('80', now=NOW-timedelta(days=1))
            with self.subTest(change=change):
                try:
                    source = self.forecast(evidence)
                except ValueError:
                    continue
                self.assertTrue(source['issues'])

    def test_api_keeps_existing_station_table_write_switch_and_manual_dispatch(self):
        from fastapi.testclient import TestClient
        from m4.settings.api import create_app
        run_id = str(uuid4())
        for enabled in ('1', '0'):
            with self.subTest(enabled=enabled), TemporaryDirectory() as root, patch.dict('os.environ', {
                    'M4_AUTO_PLAN_ENABLED': '0', 'M4_EMS_STATION2_TABLE_WRITES': enabled,
                    'M4_DECISION_RESULTS_DIR': root+'/decisions'}):
                app = create_app(Path(root)/'settings.db')
                coordinator = app.state.automatic_plans.service
                self.assertTrue(coordinator.daily.inputs.score_optional)
                self.assertEqual(coordinator.writer.status('station-2')['enabled'], enabled == '1')
                self.assertFalse(coordinator.writer.status('station-1')['enabled'])
                with patch.object(coordinator, 'start', return_value={'status': 'running'}) as begin, TestClient(app) as client:
                    result = client.post('/m4-api/stations/station-2/daily-dispatch', json={'request_id': run_id})
                    self.assertEqual(result.status_code, 202 if enabled == '1' else 409)
                    if enabled == '1':
                        begin.assert_called_once_with('station-2', replace_run_id=run_id)
                    else:
                        begin.assert_not_called()
