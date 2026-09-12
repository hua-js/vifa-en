import unittest
from m4.tests.test_m4_late_peak_reserve import reserve_request
from m4.optimizer.contracts import OptimizationRequest
from m4.optimizer.service import M4Optimizer
from m4.optimizer.validation import validate_candidate

class ScheduleBoundaryTests(unittest.TestCase):
    def test_idle_hour_and_direction_are_hard_boundaries(self):
        raw=reserve_request('peak-reserve-v3').model_dump()
        modes=['charge']*32+['discharge']*16+['idle']*8+['discharge']*16+['idle']*4+['discharge']*8+['idle']*12
        raw['ems_schedule_modes']=modes
        request=OptimizationRequest.model_validate(raw)
        result=M4Optimizer(model_version='schedule-test').optimize(request,terminal_soc_target_pct=1)
        for c in result.candidates:
            self.assertIn(c.status,('optimal','feasible'),c.solver_message)
            for allowed,p in zip(modes,c.plan):
                self.assertIn(p.mode,('idle',allowed))
            self.assertTrue(all(p.mode=='idle' for p in c.plan[72:76]))
            validate_candidate(request,c,tolerance=1e-5,terminal_soc_target_pct=1)

    def test_schedule_expansion_and_overlap(self):
        from m4.settings.schedule_power import schedule_modes
        schedule=[dict(start_time='23:00:00',end_time='01:00:00',repeat='daily',mode='charge')]
        modes=schedule_modes(schedule)
        self.assertEqual(modes[:4],['charge']*4)
        self.assertEqual(modes[4:92],['idle']*88)
        self.assertEqual(modes[92:],['charge']*4)
        with self.assertRaises(ValueError):schedule_modes(schedule*2)

    def test_short_mask_rejected(self):
        raw=reserve_request('peak-reserve-v3').model_dump()
        raw['ems_schedule_modes']=['idle']
        with self.assertRaisesRegex(ValueError,'cover every request point'):
            OptimizationRequest.model_validate(raw)

    def test_independent_validator_rejects_unscheduled_discharge(self):
        request=reserve_request('peak-reserve-v3')
        candidate=next(c for c in M4Optimizer(model_version='unbounded-test').optimize(request,terminal_soc_target_pct=1).candidates if c.status in ('optimal','feasible'))
        request.ems_schedule_modes=['idle']*96
        with self.assertRaisesRegex(ValueError,'EMS schedule direction'):
            validate_candidate(request,candidate,tolerance=1e-5,terminal_soc_target_pct=1)

    def test_idle_pv_surplus_exports_without_forced_charging(self):
        request=reserve_request('peak-reserve-v3')
        request.ems_schedule_modes=['charge']*32+['discharge']*16+['idle']*8+['discharge']*28+['idle']*12
        request.pv_dispatch_policy='load_first_storage_priority'
        request.constraints.grid_export_enabled=True
        request.constraints.grid_export_limit_kw=200
        for p in request.points[48:56]:p.pv_forecast_kw=200
        result=M4Optimizer(model_version='idle-pv-test').optimize(request,terminal_soc_target_pct=1)
        for c in result.candidates:
            self.assertIn(c.status,('optimal','feasible'),c.solver_message)
            self.assertTrue(all(p.mode=='idle' and abs(p.grid_export_kw-100)<1e-5 for p in c.plan[48:56]))
            validate_candidate(request,c,tolerance=1e-5,terminal_soc_target_pct=1)
