import json
import tempfile
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

from m4.settings import LiveStationState, ResolvedControlLimits, StationParameters, build_request
from m4.settings.api import create_app
from m4.settings.store import SettingsConflict, SettingsStore
from m4.optimizer import M4Optimizer
from m4.tests.m4_optimizer_test_support import make_request


def parameters(**changes):
    values = dict(
        energy_capacity_kwh=500.0, max_charge_kw=80.0, max_discharge_kw=90.0,
        charge_efficiency=0.94, discharge_efficiency=0.93,
        soc_min_pct=15.0, soc_max_pct=85.0,
        preferred_soc_min_pct=25.0, preferred_soc_max_pct=75.0,
        terminal_soc_tolerance_pct=4.0, grid_import_limit_kw=200.0, cycle_cost_per_kwh=0.02,
        max_input_age_seconds=300,
    )
    values.update(changes)
    return StationParameters(**values)


def resolved_controls(**changes):
    values=dict(station_id='station-1',source_version='confirmed-controls-test-v1',
        demand_limit_kw=140.0,grid_export_enabled=False,grid_export_limit_kw=0.0)
    values.update(changes)
    return ResolvedControlLimits(**values)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.sqlite3'
        self.store = SettingsStore(self.path)

    def test_unconfigured_station_has_no_mock_limits(self):
        item = self.store.get('station-1')
        self.assertIsNone(item.parameters)
        self.assertEqual(item.revision, 0)
        self.assertIsNone(item.version)

    def test_saved_parameters_survive_restart_and_stations_remain_independent(self):
        item = self.store.save('station-1', parameters(), expected_revision=0)
        loaded = SettingsStore(self.path).get('station-1')
        self.assertEqual(loaded, item)
        self.assertEqual(loaded.parameters.energy_capacity_kwh, 500)
        self.assertIsNone(self.store.get('station-2').parameters)
        self.assertTrue(item.version)

    def test_stale_editor_cannot_overwrite_newer_settings(self):
        first = self.store.save('station-1', parameters(), expected_revision=0)
        with self.assertRaises(SettingsConflict):
            self.store.save('station-1', parameters(max_charge_kw=100), expected_revision=0)
        self.assertEqual(self.store.get('station-1'), first)
        second = self.store.save('station-1', parameters(max_charge_kw=130), expected_revision=1)
        self.assertNotEqual(second.version, first.version)
        self.assertEqual(second.revision, 2)

    def test_invalid_boundaries_are_rejected(self):
        for changes in [
            {'energy_capacity_kwh':0}, {'charge_efficiency':95},
            {'max_charge_kw':-1}, {'soc_min_pct':90},
            {'preferred_soc_max_pct':95}, {'terminal_soc_tolerance_pct':101},
            {'grid_import_limit_kw':float('nan')}, {'max_discharge_kw':float('inf')},
            {'demand_limit_kw':100}, {'grid_export_enabled':False}, {'grid_export_limit_kw':0},
            {'discharge_efficiency':True}, {'max_input_age_seconds':0},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                parameters(**changes)

    def test_config_adapter_uses_live_soc_and_applies_every_parameter(self):
        config=self.store.save('station-1', parameters(), expected_revision=0)
        fixture=make_request(station_id='station-1')
        live=LiveStationState(station_id='station-1',participating_cabinet_ids=['emu11','emu12'], initial_soc_pct=61.0,
            available=True, observed_at=fixture.input_observed_at, source_version='ems-observation-1')
        request=build_request(config, live, request_id='real-request',plan_start_at=fixture.plan_start_at,
            points=fixture.points,source_versions={'load':'load-v1','pv':'baseline-v1','tariff':'tariff-v1'},profiles=fixture.profiles,control_limits=resolved_controls())
        self.assertEqual(request.capability.initial_soc_pct,61)
        self.assertEqual(request.capability.max_charge_kw,80)
        self.assertEqual(request.capability.max_discharge_kw,90)
        self.assertEqual(request.capability.energy_capacity_kwh,500)
        self.assertEqual(request.capability.charge_efficiency,.94)
        self.assertEqual(request.constraints.demand_limit_kw,140)
        self.assertEqual(request.constraints.soc_min_pct,15)
        self.assertEqual(request.constraints.preferred_soc_max_pct,75)
        self.assertEqual(request.constraints.terminal_soc_tolerance_pct,4)
        self.assertEqual(request.constraints.cycle_cost_per_kwh,.02)
        self.assertEqual(request.max_input_age_seconds,300)
        self.assertIn(config.version, request.source_versions['constraints'])
        self.assertIn('confirmed-controls-test-v1', request.source_versions['constraints'])
        self.assertEqual(request.constraints.grid_import_limit_kw,200)
        self.assertFalse(request.constraints.grid_export_enabled)
        self.assertIn('manual',request.source_versions['capability'])
        self.assertIn('ems-observation-1',request.source_versions['capability'])
        result=M4Optimizer(model_version='settings-test').optimize(request)
        self.assertTrue(any(c.status in ('optimal','feasible') for c in result.candidates))
        for c in result.candidates:
            for p in c.plan:
                self.assertLessEqual(p.target_power_kw,80.001 if p.mode=='charge' else 90.001)
                self.assertGreaterEqual(p.expected_soc_pct,14.999)
                self.assertLessEqual(p.expected_soc_pct,85.001)

    def test_adapter_blocks_missing_config_mismatched_station_and_stale_live_state(self):
        fixture=make_request(station_id='station-1')
        kwargs=dict(request_id='r',plan_start_at=fixture.plan_start_at,points=fixture.points,
            source_versions={'load':'load-v1'},profiles=fixture.profiles,control_limits=resolved_controls())
        live=LiveStationState(station_id='station-1',participating_cabinet_ids=['emu11','emu12'],initial_soc_pct=50,available=False,
            observed_at=fixture.input_observed_at,source_version='ems-1')
        with self.assertRaises(ValueError):build_request(self.store.get('station-1'),live,**kwargs)
        config=self.store.save('station-1',parameters(),expected_revision=0)
        with self.assertRaisesRegex(ValueError,'不生成'):
            build_request(config,live,**kwargs)
        live=live.model_copy(update={'available':True})
        with self.assertRaises(ValueError):
            build_request(config,live.model_copy(update={'station_id':'station-2'}),**kwargs)
        with self.assertRaises(ValueError):
            build_request(config,live.model_copy(update={'observed_at':datetime.fromisoformat('2026-09-03T20:00:00+08:00')}),**kwargs)

    def test_live_state_requires_explicit_valid_cabinet_scope(self):
        fixture=make_request(station_id='station-1')
        values=dict(station_id='station-1',initial_soc_pct=50.0,available=True,
            observed_at=fixture.input_observed_at,source_version='live-v1')
        with self.assertRaises(ValidationError):
            LiveStationState(**values)
        for scope in ([], ['emu11','emu11'], ['emu21'], ['emu27'], ['unknown'], [11]):
            with self.subTest(scope=scope), self.assertRaises(ValidationError):
                LiveStationState(**values,participating_cabinet_ids=scope)
        live=LiveStationState(**values,participating_cabinet_ids=['emu12','emu11'])
        self.assertEqual(live.participating_cabinet_ids,['emu11','emu12'])
        unavailable=LiveStationState(**{**values,'available':False},participating_cabinet_ids=[])
        self.assertFalse(unavailable.available)

    def test_partial_scope_derates_only_storage_capability_and_keeps_saved_limits(self):
        config=self.store.save('station-1',parameters(),expected_revision=0)
        before=config.model_dump()
        fixture=make_request(station_id='station-1')
        # A 190 kW load peak against the unchanged 140 kW station threshold
        # needs 50 kW: the remaining cabinet must stop at its own 45 kW limit.
        fixture=fixture.model_copy(update={'points':[
            point.model_copy(update={
                'load_forecast_kw':190.0 if index in (48,49) else 100.0,
                'pv_forecast_kw':0.0,
                'buy_price_per_kwh':.01 if index==0 else 1.0,
            }) for index,point in enumerate(fixture.points)
        ]})
        live=LiveStationState(station_id='station-1',participating_cabinet_ids=['emu12'],
            initial_soc_pct=55.0,available=True,observed_at=fixture.input_observed_at,
            source_version='same-upstream-observation')
        request=build_request(config,live,request_id='partial',plan_start_at=fixture.plan_start_at,
            points=fixture.points,source_versions={'load':'load-v1','participating_cabinets':'["emu11"]'},
            profiles=fixture.profiles,control_limits=resolved_controls())
        self.assertEqual(request.capability.energy_capacity_kwh,250)
        self.assertEqual(request.capability.max_charge_kw,40)
        self.assertEqual(request.capability.max_discharge_kw,45)
        self.assertEqual(request.capability.initial_soc_pct,55)
        self.assertEqual(request.capability.charge_efficiency,.94)
        self.assertEqual(request.capability.discharge_efficiency,.93)
        self.assertEqual(request.constraints.demand_limit_kw,140)
        self.assertEqual(request.constraints.grid_import_limit_kw,200)
        self.assertEqual(request.constraints.soc_min_pct,15)
        self.assertEqual(request.constraints.soc_max_pct,85)
        self.assertEqual(request.constraints.preferred_soc_min_pct,25)
        self.assertEqual(request.constraints.preferred_soc_max_pct,75)
        self.assertEqual(request.constraints.terminal_soc_tolerance_pct,4)
        self.assertEqual(request.constraints.cycle_cost_per_kwh,.02)
        self.assertEqual(config.model_dump(),before)
        self.assertEqual(self.store.get('station-1').model_dump(),before)
        self.assertEqual(json.loads(request.source_versions['participating_cabinets']),['emu12'])
        self.assertEqual(json.loads(request.source_versions['excluded_cabinets']),['emu11'])
        self.assertIn('等分',request.capability.derating_reason)
        self.assertIn('emu12',request.capability.derating_reason)
        self.assertIn('emu11',request.capability.derating_reason)
        result=M4Optimizer(model_version='partial-cabinet-test').optimize(request)
        self.assertTrue(any(c.status in ('optimal','feasible') for c in result.candidates))
        self.assertTrue(any(point.mode=='discharge' and point.target_power_kw>44.99
            for candidate in result.candidates for point in candidate.plan))
        self.assertTrue(any(point.mode=='charge' and point.target_power_kw>0
            for candidate in result.candidates for point in candidate.plan))
        for candidate in result.candidates:
            for point in candidate.plan:
                self.assertLessEqual(point.target_power_kw,40.001 if point.mode=='charge' else 45.001)
                self.assertGreaterEqual(point.expected_soc_pct,14.999)
                self.assertLessEqual(point.expected_soc_pct,85.001)

    def test_six_cabinet_scope_preserves_original_per_cabinet_capacity(self):
        config=self.store.save('station-2',parameters(energy_capacity_kwh=600.0,
            max_charge_kw=300.0,max_discharge_kw=240.0),expected_revision=0)
        fixture=make_request(station_id='station-2')
        live=LiveStationState(station_id='station-2',participating_cabinet_ids=['emu26','emu22'],
            initial_soc_pct=50.0,available=True,observed_at=fixture.input_observed_at,
            source_version='live-v1')
        request=build_request(config,live,request_id='partial-2',plan_start_at=fixture.plan_start_at,
            points=fixture.points,source_versions={'load':'load-v1'},profiles=fixture.profiles,
            control_limits=resolved_controls(station_id='station-2'))
        self.assertEqual(request.capability.energy_capacity_kwh,200)
        self.assertEqual(request.capability.max_charge_kw,100)
        self.assertEqual(request.capability.max_discharge_kw,80)
        self.assertEqual(json.loads(request.source_versions['participating_cabinets']),['emu22','emu26'])
        self.assertEqual(json.loads(request.source_versions['excluded_cabinets']),['emu21','emu23','emu24','emu25'])

    def test_scope_changes_request_version_and_invalid_mutations_are_revalidated(self):
        config=self.store.save('station-1',parameters(),expected_revision=0)
        fixture=make_request(station_id='station-1')
        live=LiveStationState(station_id='station-1',participating_cabinet_ids=['emu11','emu12'],
            initial_soc_pct=50.0,available=True,observed_at=fixture.input_observed_at,
            source_version='same-upstream')
        kwargs=dict(request_id='r',plan_start_at=fixture.plan_start_at,points=fixture.points,
            source_versions={'load':'load-v1'},profiles=fixture.profiles,control_limits=resolved_controls())
        full=build_request(config,live,**kwargs)
        left=build_request(config,live.model_copy(update={'participating_cabinet_ids':['emu11']}),**kwargs)
        right=build_request(config,live.model_copy(update={'participating_cabinet_ids':['emu12']}),**kwargs)
        reversed_full=build_request(config,live.model_copy(update={'participating_cabinet_ids':['emu12','emu11']}),**kwargs)
        self.assertEqual(full.request_id,reversed_full.request_id)
        self.assertEqual(full.source_versions,reversed_full.source_versions)
        self.assertEqual(len({full.request_id,left.request_id,right.request_id}),3)
        self.assertEqual(len({full.source_versions['capability'],left.source_versions['capability'],right.source_versions['capability']}),3)
        for scope in ([],['emu11','emu11'],['emu21']):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                build_request(config,live.model_copy(update={'participating_cabinet_ids':scope}),**kwargs)
        with self.assertRaises(ValueError):
            build_request(config,live.model_copy(update={'available':False,'participating_cabinet_ids':[]}),**kwargs)

    def test_removed_fields_do_not_survive_as_manual_controls_in_legacy_storage(self):
        old=self.store.save('station-1',parameters(),expected_revision=0).model_dump(mode='json')
        old['parameters'].update(demand_limit_kw=999,grid_export_enabled=True,grid_export_limit_kw=123)
        raw=json.dumps(old)
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE m4_station_settings SET document=? WHERE station_id=?',(raw,'station-1'))
        loaded=self.store.get('station-1')
        self.assertNotIn('demand_limit_kw',loaded.parameters.model_dump())
        self.assertEqual(loaded.parameters.energy_capacity_kwh,500)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT document FROM m4_station_settings').fetchone()[0],raw)

    def test_missing_controls_block_and_source_changes_request_version(self):
        config=self.store.save('station-1',parameters(),expected_revision=0)
        fixture=make_request(station_id='station-1')
        live=LiveStationState(station_id='station-1',participating_cabinet_ids=['emu11','emu12'],initial_soc_pct=50,available=True,
            observed_at=fixture.input_observed_at,source_version='live-v1')
        kwargs=dict(request_id='r',plan_start_at=fixture.plan_start_at,points=fixture.points,
            source_versions={'load':'v1'},profiles=fixture.profiles)
        with self.assertRaisesRegex(ValueError,'控制来源'):
            build_request(config,live,**kwargs)
        with self.assertRaisesRegex(ValueError,'同一电站'):
            build_request(config,live,control_limits=resolved_controls(station_id='station-2'),**kwargs)
        first=build_request(config,live,control_limits=resolved_controls(),**kwargs)
        second=build_request(config,live,control_limits=resolved_controls(source_version='controls-v2',demand_limit_kw=150),**kwargs)
        self.assertNotEqual(first.request_id,second.request_id)
        self.assertNotEqual(first.source_versions['constraints'],second.source_versions['constraints'])
        self.assertEqual(second.constraints.demand_limit_kw,150)
        self.assertEqual(second.constraints.grid_import_limit_kw,200)

    def test_control_api_separate_from_manual_storage_and_does_not_fall_back(self):
        from m4.settings.control_sources import ControlSourceError
        class Reader:
            def fetch(self,station_id):
                if station_id=='station-2':raise ControlSourceError('本站需量配置缺失')
                return dict(station_id=station_id,status='ready',demand={'need_kw':1062.5})
        with TestClient(create_app(self.path,control_reader=Reader())) as client:
            route='/m4-api/stations/station-1'
            response=client.get(route+'/control-sources')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['demand']['need_kw'],1062.5)
            self.assertIsNone(client.get(route+'/settings').json()['parameters'])
            self.assertEqual(client.get('/m4-api/stations/station-2/control-sources').status_code,502)
            self.assertEqual(client.get('/m4-api/stations/unknown/control-sources').status_code,404)
            self.assertEqual(client.put(route+'/control-sources',json={}).status_code,405)

    def test_http_read_save_conflict_and_validation(self):
        with TestClient(create_app(self.path)) as client:
            route='/m4-api/stations/station-1/settings'
            self.assertIsNone(client.get(route).json()['parameters'])
            body={'expected_revision':0,'parameters':parameters().model_dump()}
            self.assertEqual(client.put(route,json=body).status_code,200)
            self.assertEqual(client.put(route,json=body).status_code,409)
            body['expected_revision']=1;body['parameters']['soc_max_pct']=10
            self.assertEqual(client.put(route,json=body).status_code,422)
            self.assertEqual(client.get(route).json()['revision'],1)
            self.assertEqual(client.get('/m4-api/stations/unknown/settings').status_code,404)
            self.assertEqual(client.get('/m4').status_code,200)


if __name__=='__main__':unittest.main()
