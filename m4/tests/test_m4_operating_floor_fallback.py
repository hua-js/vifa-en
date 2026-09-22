"""Regression: an EMS discharge schedule must not spend the rolling floor."""
import copy
import json
import unittest
from datetime import datetime, timedelta
from m4.optimizer.contracts import OptimizationRequest
from m4.settings.objectives import get_daily_profiles
from m4.settings.terminal_policy import POLICY, remaining_reference, spends_floor_from_empty, idle_floor_tolerance
from m4.settings.ems_model_update import validate_dispatch_safety, ModelUpdateError
from m4.tests.m4_optimizer_test_support import make_request


class OperatingFloorFallbackTests(unittest.TestCase):
    def request(self, soc=2.0, load=50.0, pv=0.0):
        r=make_request(station_id='station-2').model_dump(mode='json')
        start=datetime.fromisoformat('2026-09-21T23:00:00+08:00')
        r.update(plan_start_at=start.isoformat(),input_observed_at=start.isoformat(),horizon_points=4,
            peak_reserve_policy=dict(version='peak-reserve-v4',terminal_soc_min_pct=2.0),
            pv_dispatch_policy='load_first_export_priority')
        r['profiles']=[p.model_dump(mode='json') for p in get_daily_profiles('station-2')]
        r['source_versions'].update(terminal_policy=POLICY,terminal_inventory_price='0.3',planning_basis='remaining-day-fixed-baseline-v4')
        r['capability']['initial_soc_pct']=soc
        r['constraints'].update(soc_min_pct=1.0,preferred_soc_min_pct=2.0,demand_limit_kw=100.0,grid_import_limit_kw=100.0,grid_export_enabled=True,grid_export_limit_kw=100.0)
        r['points']=r['points'][:4]
        for i,p in enumerate(r['points']):p.update(timestamp=(start+timedelta(minutes=15*i)).isoformat(),tariff_period='feng',load_forecast_kw=load,pv_forecast_kw=pv)
        return OptimizationRequest.model_validate_json(json.dumps(r))

    def replay(self,r):
        schedule=[dict(start_time='00:00:00',end_time='24:00:00',mode='discharge',power_kw=50.0,repeat='daily',soc_min_pct=1.0,soc_max_pct=98.0)]
        before=copy.deepcopy(schedule)
        plan,sim=remaining_reference(r,r.capability,r.points,schedule)
        self.assertEqual(schedule,before)
        return plan,sim

    def validate(self,r,plan):
        validate_dispatch_safety(r.model_dump(mode='json'),[p.model_dump(mode='json') for p in plan])

    def test_at_floor_is_safe_idle(self):
        r=self.request();plan,sim=self.replay(r)
        self.assertTrue(all(p.mode=='idle' for p in plan))
        self.assertAlmostEqual(plan[-1].expected_soc_pct,2.0)
        self.assertEqual(sim['operating_soc_floor_pct'],2.0)
        self.validate(r,plan)

    def test_above_floor_can_discharge_only_available_energy(self):
        r=self.request(soc=3.0);plan,_=self.replay(r)
        self.assertTrue(any(p.mode=='discharge' for p in plan))
        self.assertTrue(all(p.expected_soc_pct>=2.0-1e-6 for p in plan))
        self.validate(r,plan)

    def test_demand_shortfall_is_not_reported_as_safe_idle(self):
        r=self.request(load=150.0);plan,_=self.replay(r)
        with self.assertRaises(ModelUpdateError):self.validate(r,plan)

    def test_below_floor_without_recovery_still_blocks(self):
        r=self.request(soc=1.5);plan,_=self.replay(r)
        with self.assertRaises(ModelUpdateError):self.validate(r,plan)

    def test_surplus_still_exports(self):
        r=self.request(pv=90.0);plan,_=self.replay(r)
        self.assertTrue(all(p.mode=='idle' and p.grid_export_kw==40.0 for p in plan))
        self.validate(r,plan)

    def test_historical_reference_retains_original_floor(self):
        r=self.request();r.source_versions.pop('terminal_policy')
        plan,sim=self.replay(r)
        self.assertLess(plan[-1].expected_soc_pct,2.0)
        self.assertNotIn('operating_soc_floor_pct',sim)

    def test_borrowing_floor_then_recharging_is_not_a_safe_candidate(self):
        r=self.request()
        plan,_=self.replay(r)
        plan[0]=plan[0].model_copy(update={'expected_soc_pct':1.5})
        self.assertTrue(spends_floor_from_empty(r,plan))
        r.capability.initial_soc_pct=3.0
        self.assertFalse(spends_floor_from_empty(r,plan))

    def test_idle_tolerance_includes_reported_soc_and_boundary(self):
        for soc in (1.975, 1.95, 2.0):
            with self.subTest(soc=soc):
                r=self.request(soc=soc);plan,_=self.replay(r)
                self.assertTrue(idle_floor_tolerance(r,plan))
                self.assertFalse(spends_floor_from_empty(r,plan))
                self.validate(r,plan)
                self.assertAlmostEqual(plan[-1].expected_soc_pct,soc)

    def test_idle_beyond_tolerance_is_still_blocked(self):
        r=self.request(soc=1.949);plan,_=self.replay(r)
        self.assertFalse(idle_floor_tolerance(r,plan))
        with self.assertRaises(ModelUpdateError):self.validate(r,plan)

    def test_tolerance_cannot_be_spent_even_with_forged_soc(self):
        r=self.request(soc=1.975);plan,_=self.replay(r)
        plan[0]=plan[0].model_copy(update={'mode':'discharge','target_power_kw':0.1})
        self.assertFalse(idle_floor_tolerance(r,plan))
        self.assertTrue(spends_floor_from_empty(r,plan))
        with self.assertRaises(ModelUpdateError):self.validate(r,plan)
