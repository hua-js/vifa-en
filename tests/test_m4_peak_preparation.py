"""Read-only peak preparation arithmetic, without running an optimizer."""
import unittest
from m4_optimizer.contracts import CandidateResult, PlanPoint
from m4_optimizer.metrics import calculate_metrics
from tests.m4_optimizer_test_support import make_request
from m4_settings.peak_preparation import summarize_peak_preparation


def scenario(loads=None, pv=None, charges=None, **limits):
    request = make_request()
    request.capability = request.capability.model_copy(update=dict(
        energy_capacity_kwh=100.0, initial_soc_pct=limits.get('soc', 20.0),
        charge_efficiency=limits.get('eta_c', .8), discharge_efficiency=limits.get('eta_d', .8),
        max_charge_kw=limits.get('charge', 50.0), max_discharge_kw=limits.get('discharge', 100.0)))
    request.constraints = request.constraints.model_copy(update=dict(
        demand_limit_kw=100.0, grid_import_limit_kw=1000.0, soc_min_pct=10.0,
        soc_max_pct=90.0, preferred_soc_min_pct=10.0, preferred_soc_max_pct=90.0,
        terminal_soc_tolerance_pct=100.0))
    request.points = [p.model_copy(update={'load_forecast_kw':float((loads or {}).get(i, 50)),
        'pv_forecast_kw':float((pv or {}).get(i, 0))}) for i,p in enumerate(request.points)]
    energy = request.capability.initial_soc_pct
    plan = []
    for i,p in enumerate(request.points):
        charge = (charges or {}).get(i, 0.0)
        energy += charge*.25*request.capability.charge_efficiency
        grid = p.load_forecast_kw-p.pv_forecast_kw+charge
        plan.append(PlanPoint(timestamp=p.timestamp, mode='charge' if charge else 'idle',
            target_power_kw=float(charge), expected_soc_pct=energy, grid_import_kw=grid,
            grid_export_kw=0.0, pv_unabsorbed_kw=0.0, demand_exceed_kw=max(0, grid-100)))
    candidate = CandidateResult(profile_id='balanced', profile_version='test-balanced-v1',
        plan_version='test-plan-v1', status='feasible', solver_message='test plan', solve_seconds=0.0,
        plan=plan, metrics=calculate_metrics(request, plan), layers=[], risk_codes=[], risk_messages=[])
    return request,candidate


class PeakPreparationTests(unittest.TestCase):
    def test_no_peak_has_no_artificial_soc_target(self):
        request,candidate=scenario()
        view=summarize_peak_preparation(request,candidate)
        self.assertEqual(view['windows'],[])
        self.assertEqual(view['status'],'clear')
        self.assertEqual(view['plan_version'],candidate.plan_version)
        self.assertEqual(view['station_id'],request.station_id)

    def test_efficiencies_and_battery_side_energy_are_explicit(self):
        view=summarize_peak_preparation(*scenario({0:140,1:140}))
        window=view['windows'][0]
        self.assertAlmostEqual(window['required_discharge_kwh'],20)
        self.assertAlmostEqual(window['required_battery_kwh'],25)
        self.assertAlmostEqual(window['planned_available_kwh'],10)
        self.assertAlmostEqual(window['energy_gap_kwh'],15)
        self.assertAlmostEqual(window['required_start_soc_pct'],35)
        self.assertAlmostEqual(window['additional_charge_kwh'],18.75)
        self.assertAlmostEqual(window['minimum_charge_minutes'],22.5)
        self.assertEqual(view['status'],'attention')

    def test_pv_offsets_load_and_separate_windows_do_not_share_initial_soc(self):
        request,candidate=scenario({1:140,2:140,4:140},pv={2:50},charges={0:40})
        view=summarize_peak_preparation(request,candidate)
        self.assertEqual([(w['start_index'],w['end_index']) for w in view['windows']],[(1,2),(4,5)])
        self.assertAlmostEqual(view['windows'][0]['planned_start_soc_pct'],28)
        self.assertAlmostEqual(view['windows'][0]['planned_available_kwh'],18)
        self.assertEqual(view['windows'][0]['energy_gap_kwh'],0)
        self.assertGreater(view['windows'][0]['predicted_exceed_kw'],0)

    def test_power_shortage_is_not_misreported_as_energy_shortage(self):
        view=summarize_peak_preparation(*scenario({0:300},soc=90,eta_d=1))
        window=view['windows'][0]
        self.assertEqual(window['energy_gap_kwh'],0)
        self.assertEqual(window['power_gap_kw'],100)
        self.assertEqual(view['status'],'attention')

    def test_capacity_shortage_and_zero_charge_power_do_not_suggest_a_time(self):
        window=summarize_peak_preparation(*scenario({0:500},charge=0))['windows'][0]
        self.assertGreater(window['required_start_soc_pct'],90)
        self.assertGreater(window['capacity_gap_kwh'],0)
        self.assertIsNone(window['minimum_charge_minutes'])

    def test_solver_tolerance_does_not_create_a_peak_window(self):
        view=summarize_peak_preparation(*scenario({0:100.000001}))
        self.assertEqual(view['windows'],[])
        self.assertEqual(view['status'],'clear')

    def test_charge_induced_exceedance_still_shows_attention_without_load_peak(self):
        view=summarize_peak_preparation(*scenario({0:95},charges={0:40}))
        self.assertEqual(view['windows'],[])
        self.assertEqual(view['status'],'attention')
        self.assertEqual(view['predicted_exceed_kw'],35)

    def test_invalid_or_unusable_plan_is_rejected(self):
        request,candidate=scenario({0:140})
        candidate.plan[0].expected_soc_pct=99
        with self.assertRaises(ValueError):summarize_peak_preparation(request,candidate)
        request,candidate=scenario()
        candidate.status='infeasible';candidate.plan=[];candidate.metrics=None
        with self.assertRaises(ValueError):summarize_peak_preparation(request,candidate)


if __name__=='__main__':unittest.main()
