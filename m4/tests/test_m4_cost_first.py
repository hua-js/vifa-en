import unittest
from m4.settings.objectives import get_daily_profiles
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.service import M4Optimizer
from m4.tests.m4_optimizer_test_support import make_request

class CostFirstTests(unittest.TestCase):
    def test_station_one_daily_cost_precedes_demand(self):
        for profile in get_daily_profiles('station-1'):
            self.assertEqual(profile.objective_order[0].terms, {'energy_cost': 1.0})
        self.assertEqual(get_daily_profiles('station-2')[0].objective_order[0].terms, {'demand_peak': 1.0})

    def test_flat_demand_target_does_not_consume_peak_energy(self):
        r=make_request()
        r.station_id='station-1'
        r.constraints.grid_import_limit_kw=550
        r.constraints.demand_limit_kw=504
        r.source_versions.update(physical_grid_policy='station-1-550-v1', economic_policy='station-1-cost-first-v1')
        r.profiles=get_daily_profiles('station-1')
        for p in r.points:
            p.load_forecast_kw=0;p.pv_forecast_kw=0;p.buy_price_per_kwh=0.66
        r.points[32].load_forecast_kw=526
        r.points[72].load_forecast_kw=500;r.points[72].buy_price_per_kwh=1.1
        r.capability.max_charge_kw=0
        r.capability.energy_capacity_kwh=100
        r.capability.initial_soc_pct=40
        r=OptimizationRequest.model_validate(r.model_dump())
        c=next(c for c in M4Optimizer(model_version='cost-first-test').optimize(r).candidates if c.profile_id=='cost')
        self.assertEqual(c.status,'optimal')
        self.assertLess(c.plan[32].target_power_kw,1e-4)
        self.assertGreater(c.plan[72].target_power_kw,1)
        self.assertTrue(all(p.grid_import_kw<=550+1e-5 for p in c.plan))

    def test_cost_first_cannot_change_station_or_ceiling(self):
        r=make_request().model_dump()
        r.update(station_id='station-1', profiles=[p.model_dump() for p in get_daily_profiles('station-1')])
        r['source_versions'].update(economic_policy='station-1-cost-first-v1',physical_grid_policy='station-1-550-v1')
        r['constraints']['grid_import_limit_kw']=550
        OptimizationRequest.model_validate(r)
        for key, value in [('station_id','station-2'), ('grid_import_limit_kw',600)]:
            import copy
            invalid=copy.deepcopy(r)
            if key=='station_id': invalid[key]=value
            else: invalid['constraints'][key]=value
            with self.assertRaisesRegex(ValueError,'physical ceiling'):
                OptimizationRequest.model_validate(invalid)
