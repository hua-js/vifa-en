"""Economic discharge is allowed after peaks without borrowing terminal reserve."""
import unittest
from m4.tests.test_m4_late_peak_reserve import reserve_request
from m4.optimizer.model import build_model
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import validate_candidate
from m4.settings.objectives import get_daily_profiles

class EconomicReserveTests(unittest.TestCase):
    def test_v3_allows_profitable_evening_flat_discharge(self):
        request = reserve_request('peak-reserve-v3')
        for i, p in enumerate(request.points):
            p.load_forecast_kw = 5 if i < 76 else 100
        candidate = next(c for c in M4Optimizer(model_version="economic-v3-regression").optimize(request, terminal_soc_target_pct=1).candidates if c.profile_id == 'cost')
        self.assertEqual(candidate.status, 'optimal', candidate.solver_message)
        self.assertGreater(sum(p.target_power_kw*.25 for p in candidate.plan[76:84] if p.mode=='discharge'), 1)
        self.assertGreaterEqual(candidate.plan[-1].expected_soc_pct, 2-1e-5)
        validate_candidate(request, candidate, tolerance=1e-5, terminal_soc_target_pct=1)

    def test_v2_keeps_nonpeak_discharge_prohibition(self):
        old=build_model(reserve_request('peak-reserve-v2')).problem.model
        new=build_model(reserve_request('peak-reserve-v3')).problem.model
        self.assertEqual(old.discharge[76].ub, 0)
        self.assertGreater(new.discharge[76].ub, 0)

    def test_cost_precedes_peak_preparation(self):
        cost=next(p for p in get_daily_profiles('station-2') if p.profile_id=='cost')
        names=[x.name for x in cost.objective_order]
        self.assertLess(names.index('energy-cost'),names.index('peak-reserve-shortfall'))

    def test_v3_rejects_reserve_before_cost(self):
        from m4.optimizer.contracts import OptimizationRequest
        request=reserve_request('peak-reserve-v3').model_dump()
        layers=request['profiles'][1]['objective_order']
        reserve=next(x for x in layers if x['name']=='peak-reserve-shortfall')
        layers.remove(reserve)
        layers.insert(2,reserve)
        with self.assertRaisesRegex(ValueError,'economic cost must precede'):
            OptimizationRequest.model_validate(request)
