import unittest
from m4.settings.runtime_config import runtime_parameters
from m4.tests.m4_optimizer_test_support import make_request
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import validate_candidate

class PhysicalImportTests(unittest.TestCase):
    def test_station_one_physical_limit_is_separate(self):
        self.assertEqual(runtime_parameters('station-1')['grid_import_limit_kw'],550)
        self.assertIsNone(runtime_parameters('station-2')['grid_import_limit_kw'])

    def test_unavoidable_demand_exceedance_is_feasible_below_physical_limit(self):
        r=make_request();r.constraints.demand_limit_kw=504;r.constraints.grid_import_limit_kw=550
        r.capability.max_discharge_kw=120
        for p in r.points:p.load_forecast_kw=100;p.pv_forecast_kw=0
        r.points[40].load_forecast_kw=641.44
        result=M4Optimizer(model_version='physical-limit-test').optimize(r)
        for c in result.candidates:
            self.assertIn(c.status,('optimal','feasible'),c.solver_message)
            self.assertAlmostEqual(c.plan[40].grid_import_kw,521.44,places=3)
            self.assertTrue(all(p.grid_import_kw<=550+1e-5 for p in c.plan))
            validate_candidate(r,c,tolerance=1e-5)

    def test_physical_ceiling_cannot_be_relaxed(self):
        r=make_request();r.constraints.demand_limit_kw=504;r.constraints.grid_import_limit_kw=550
        r.capability.max_discharge_kw=120
        r.points[40].load_forecast_kw=700;r.points[40].pv_forecast_kw=0
        result=M4Optimizer(model_version='hard-ceiling-test').optimize(r)
        self.assertTrue(all(c.status=='infeasible' for c in result.candidates))
