import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from m4.optimizer import M4Optimizer
from m4.settings.api import create_app
from m4.settings.candidates import CandidateService, CandidateError
from m4.settings.live_inputs import LiveInputService
from m4.settings.store import SettingsStore, SettingsConflict
from m4.tests.test_m4_live_inputs import Client, Controls, NOW
from m4.tests.test_m4_realtime import configuration
from m4.settings.selection import PolicyPreferences, PolicyStore, LiveSelectionService


def preferences(**overrides):
    values=dict(metric='energy_cost',demand_peak_tolerance_kw=0.000001,
                demand_energy_tolerance_kwh=0.000001,metric_tolerance=0.000001,
                tie_order=['cost','balanced','pv'])
    values.update(overrides)
    return PolicyPreferences(**values)


class LiveSelectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'settings.db';self.store=SettingsStore(self.path)
        self.config=self.store.save('station-1',configuration().parameters,expected_revision=0)
        self.policies=PolicyStore(self.path);self.client=Client();self.controls=Controls();self.now=NOW
        self.inputs=LiveInputService(self.client,self.controls)
        self.fetch=lambda config:self.inputs.fetch(config,now=self.now)
        self.candidates=CandidateService(store=self.store,fetch_inputs=self.fetch,
            read_controls=self.controls.fetch,optimizer=M4Optimizer(model_version='selection-test'),clock=lambda:self.now)
        self.service=LiveSelectionService(store=self.store,policies=self.policies,
            candidates=self.candidates,fetch_inputs=self.fetch,clock=lambda:self.now)
        self.envelope=None

    def calculate(self):
        self.envelope=self.candidates.calculate('station-1',self.config.version)
        return self.envelope

    def select(self, revision=0):
        return self.service.select('station-1',self.envelope['result']['run_id'],revision)

    def test_policy_cas_clear_station_isolation_and_no_settings_change(self):
        self.assertIsNone(self.policies.get('station-1').policy)
        saved=self.policies.save('station-1',preferences(),expected_revision=0)
        self.assertEqual(saved.revision,1)
        self.assertEqual(saved.policy.version,saved.version)
        self.assertEqual(self.policies.get('station-1'),saved)
        self.assertIsNone(self.policies.get('station-2').policy)
        with self.assertRaises(SettingsConflict):self.policies.save('station-1',None,expected_revision=0)
        cleared=self.policies.save('station-1',None,expected_revision=1)
        self.assertEqual(cleared.revision,2);self.assertIsNone(cleared.policy)
        self.assertEqual(self.store.get('station-1'),self.config)

    def test_selection_missing_policy_then_explicit_preview_without_resolving(self):
        before=copy.deepcopy(self.calculate())
        self.assertEqual(self.select().selection.status,'pending_policy')
        self.policies.save('station-1',preferences(),expected_revision=0)
        response=self.select(1)
        self.assertEqual(response.selection.status,'selected')
        self.assertEqual(response.candidate_run_id,before['result']['run_id'])
        self.assertEqual(response.selection.dispatch_status,'not_dispatched')
        self.assertEqual(self.candidates.latest('station-1'),before)
        self.assertEqual(before['result']['stations'][0]['selection_status'],'pending_selection')

    def test_old_revision_or_run_is_rejected(self):
        self.calculate();self.policies.save('station-1',preferences(),expected_revision=0)
        with self.assertRaises(CandidateError) as caught:self.select(0)
        self.assertEqual(caught.exception.status_code,409)
        with self.assertRaises(CandidateError):self.service.select('station-1','old-run',1)

    def test_expiration_during_mathematical_selection_rejects_the_result(self):
        from datetime import datetime
        from m4.selection import solver_selection
        self.calculate()
        self.policies.save('station-1', preferences(), expected_revision=0)
        solve = solver_selection.solve_pyomo_model

        def solve_and_expire(*args, **kwargs):
            result = solve(*args, **kwargs)
            self.now = datetime.fromisoformat(self.envelope['expires_at'])
            return result

        with patch.object(solver_selection, 'solve_pyomo_model', side_effect=solve_and_expire):
            with self.assertRaises(CandidateError) as caught:
                self.select(1)
        self.assertEqual(caught.exception.status_code, 409)

    def test_expired_and_changed_inputs_cannot_select(self):
        self.calculate()
        changes=[lambda:setattr(self,'now',NOW+timedelta(minutes=2)),
                 lambda:self.client.devices[0].update(latest_soc=49.0),
                 lambda:self.client.devices[0].update(alert_status='alert'),
                 lambda:self.controls.result.update(version='new-control'),
                 lambda:self.client.rates[0].update(fprice=2.0,hprice=2.0,vprice=2.0)]
        for change in changes:
            with self.subTest(change=change):
                old_devices=copy.deepcopy(self.client.devices);old_controls=copy.deepcopy(self.controls.result);old_rates=copy.deepcopy(self.client.rates)
                change()
                with self.assertRaises(CandidateError):self.select()
                self.now=NOW;self.client.devices=old_devices;self.controls.result=old_controls;self.client.rates=old_rates

    def test_changed_policy_or_inputs_during_selection_rejected(self):
        self.calculate()
        from m4.selection import select_candidate
        for mutation in [lambda:self.policies.save('station-1',preferences(),expected_revision=0),
                         lambda:self.client.devices[0].update(alert_status='alert')]:
            with self.subTest(mutation=mutation):
                revision=self.policies.get('station-1').revision
                def select_and_change(*args):
                    result=select_candidate(*args);mutation();return result
                with patch('m4.settings.selection.select_candidate',side_effect=select_and_change):
                    with self.assertRaises(CandidateError):self.select(revision)

    def test_new_candidate_during_selection_rejected(self):
        self.calculate()
        from m4.selection import select_candidate
        def select_and_change(*args):
            result=select_candidate(*args)
            self.candidates._results['station-1']['result']['run_id']='new-run'
            return result
        with patch('m4.settings.selection.select_candidate',side_effect=select_and_change):
            with self.assertRaises(CandidateError):self.select()

    def test_api_policy_selection_errors_and_origin(self):
        app=create_app(self.path,control_reader=self.controls,input_service=self.inputs,
                       candidate_service=self.candidates,selection_service=self.service)
        with TestClient(app) as client:
            endpoint='/m4-api/stations/station-1/selection-policy'
            self.assertIsNone(client.get(endpoint).json()['policy'])
            body=dict(expected_revision=0,preferences=preferences().model_dump())
            self.assertEqual(client.put(endpoint,json=body,headers={'Origin':'https://wrong.test'}).status_code,403)
            saved=client.put(endpoint,json=body);self.assertEqual(saved.status_code,200)
            self.assertEqual(client.put(endpoint,json=body).status_code,409)
            self.calculate()
            target='/m4-api/stations/station-1/selection'
            payload=dict(candidate_run_id=self.envelope['result']['run_id'],policy_revision=1)
            response=client.post(target,json=payload)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['selection']['status'],'selected')
            self.assertEqual(client.post(target,json={**payload,'plan':[]}).status_code,422)
            self.assertEqual(client.get('/m4-api/stations/unknown/selection-policy').status_code,404)
            self.assertEqual(client.put(endpoint,json=dict(expected_revision=1,preferences=None)).status_code,200)

    def test_api_profile_priority_roundtrip_and_invalid_rank_tolerance(self):
        app=create_app(self.path,control_reader=self.controls,input_service=self.inputs,
                       candidate_service=self.candidates,selection_service=self.service)
        with TestClient(app) as client:
            endpoint='/m4-api/stations/station-1/selection-policy'
            values=preferences().model_dump()
            values.update(metric='profile_priority',metric_tolerance=1.0,
                          tie_order=['balanced','cost','pv'])
            response=client.put(endpoint,json=dict(expected_revision=0,preferences=values))
            self.assertEqual(response.status_code,422,response.text)
            self.assertEqual(client.get(endpoint).json()['revision'],0)
            values.update(metric_tolerance=0.0,demand_peak_tolerance_kw=0.0,demand_energy_tolerance_kwh=0.0)
            saved=client.put(endpoint,json=dict(expected_revision=0,preferences=values))
            self.assertEqual(saved.status_code,200,saved.text)
            self.assertEqual(client.get(endpoint).json()['policy']['metric'],'profile_priority')
            self.calculate()
            selected=client.post('/m4-api/stations/station-1/selection',json=dict(
                candidate_run_id=self.envelope['result']['run_id'],policy_revision=1))
            self.assertEqual(selected.status_code,200,selected.text)
            decision=selected.json()['selection']
            self.assertEqual(decision['selector_version'],'pyomo-highs-selection-v2')
            self.assertEqual(decision['steps'][-1]['metric'],'profile_priority')
            remaining=decision['steps'][1]['remaining_ids']
            self.assertEqual(decision['selected']['profile_id'],next(pid for pid in values['tie_order'] if pid in remaining))
            self.assertEqual(decision['dispatch_status'],'not_dispatched')
            self.assertEqual(self.store.get('station-1'),self.config)
            self.assertIsNone(client.get('/m4-api/stations/station-2/selection-policy').json()['policy'])

    def test_read_failure_does_not_return_previous_selection_or_leak_error(self):
        self.calculate();self.client.failures.add('t_emu')
        with self.assertRaises(CandidateError) as caught:self.select()
        self.assertNotIn('secret',str(caught.exception))
        self.assertEqual(caught.exception.status_code,502)

    def test_redistributed_cabinet_soc_is_not_only_a_timestamp_refresh(self):
        self.calculate()
        self.client.devices[0]['latest_soc']=49.0
        self.client.devices[1]['latest_soc']=51.0
        with self.assertRaises(CandidateError) as caught:self.select()
        self.assertEqual(caught.exception.status_code,409)

    def test_only_sample_time_change_keeps_original_expiry(self):
        original=self.calculate()
        self.now=NOW+timedelta(seconds=5)
        for device in self.client.devices:device['last_time_iso']=(self.now-timedelta(seconds=20)).isoformat()
        response=self.select()
        self.assertEqual(response.expires_at.isoformat(),original['expires_at'])
        self.assertEqual(response.selection.status,'pending_policy')
