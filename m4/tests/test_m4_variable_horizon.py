"""Optimizer-only 95-point compatibility; current M3 input windows are tested separately."""
import unittest
from datetime import timedelta

from m4.optimizer import M4Optimizer
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.model import build_model
from m4.optimizer.validation import validate_candidate
from m4.settings.peak_preparation import summarize_peak_preparation
from m4.tests.m4_optimizer_test_support import make_request


class VariableHorizonTests(unittest.TestCase):


    def test_95_point_model_solves_and_terminal_soc_and_cost_use_actual_end(self):
        original=make_request()
        payload=original.model_dump();payload.update(horizon_points=95,points=payload['points'][:95])
        request=OptimizationRequest.model_validate(payload)
        built=build_model(request)
        self.assertEqual(built.index.charge.stop-built.index.charge.start,95)
        self.assertEqual(built.index.energy.stop-built.index.energy.start,96)
        result=M4Optimizer(model_version='test-window').optimize(request)
        for candidate in result.candidates:
            self.assertIn(candidate.status,('optimal','feasible'))
            self.assertEqual(len(candidate.plan),95)
            validate_candidate(request,candidate)
            cost=sum((p.grid_import_kw*f.buy_price_per_kwh-p.grid_export_kw*f.sell_price_per_kwh)*.25
                     for p,f in zip(candidate.plan,request.points))
            self.assertAlmostEqual(candidate.metrics.energy_cost,cost,places=5)
            self.assertLessEqual(abs(candidate.plan[-1].expected_soc_pct-request.capability.initial_soc_pct),
                                 request.constraints.terminal_soc_tolerance_pct+1e-4)
            summary=summarize_peak_preparation(request,candidate)
            self.assertEqual(summary['plan_end_at'],(request.plan_start_at+timedelta(minutes=1425)).isoformat())
        for count in (94,97):
            with self.subTest(count=count),self.assertRaises(ValueError):
                OptimizationRequest.model_validate({**payload,'horizon_points':count})

    def test_95_point_valley_labels_do_not_disable_a_fully_known_overnight_block(self):
        from m4.tests.test_m4_optimizer_early_valley import valley_request
        original=valley_request(start_hour=22)
        payload=original.model_dump();payload.update(horizon_points=95,points=payload['points'][:95])
        built=build_model(OptimizationRequest.model_validate(payload))
        self.assertEqual(built.valley_charge_windows,(tuple(range(8,40)),))

    def test_95_point_pv_policy_uses_dynamic_binary_variables(self):
        payload=make_request().model_dump()
        payload.update(horizon_points=95,points=payload['points'][:95],pv_dispatch_policy='load_first_economic')
        for point in payload['points'][40:48]:point['pv_forecast_kw']=150.0
        request=OptimizationRequest.model_validate(payload)
        result=M4Optimizer(model_version='test-window-pv').optimize(request)
        for candidate in result.candidates:
            self.assertIn(candidate.status,('optimal','feasible'))
            validate_candidate(request,candidate)
            self.assertEqual(len(candidate.plan),95)


if __name__=='__main__':unittest.main()
