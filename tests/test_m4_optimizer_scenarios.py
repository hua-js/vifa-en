import unittest

from m4_optimizer import M4Optimizer
from tests.m4_optimizer_test_support import (
    candidate_by_id,
    make_demand_peak_request,
    make_midday_pv_request,
    make_price_arbitrage_request,
    make_request,
    no_storage_unused_pv_energy,
    sum_power,
)


def metric_objective_value(metrics, layer):
    values = {
        "demand_peak": metrics.peak_demand_exceed_kw,
        "demand_duration": metrics.demand_exceed_energy_kwh,
        "soc_preferred_deviation": metrics.preferred_soc_deviation,
        "energy_cost": metrics.energy_cost,
        "pv_unused": (
            metrics.grid_export_energy_kwh
            + metrics.pv_unabsorbed_energy_kwh
        ),
        "throughput": metrics.throughput_energy_kwh,
    }
    return sum(weight * values[name] for name, weight in layer.terms.items())


class M4OptimizerScenarioTests(unittest.TestCase):
    def test_optimizer_returns_three_candidates_in_fixed_order(self):
        source_request = make_request()
        request = make_request(
            station_id="station-1",
            profiles=list(reversed(source_request.profiles)),
        )

        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)

        self.assertEqual(result.station_id, "station-1")
        self.assertEqual(
            [candidate.profile_id for candidate in result.candidates],
            ["balanced", "cost", "pv"],
        )
        self.assertTrue(
            all(candidate.status in {"optimal", "feasible"} for candidate in result.candidates)
        )
        self.assertTrue(all(len(candidate.plan) == 96 for candidate in result.candidates))

    def test_each_candidate_keeps_its_profile_version(self):
        request = make_request()

        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)

        expected = {profile.profile_id: profile.profile_version for profile in request.profiles}
        self.assertEqual(
            {item.profile_id: item.profile_version for item in result.candidates},
            expected,
        )

    def test_cost_profile_charges_in_valley_and_discharges_at_peak(self):
        request = make_price_arbitrage_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "cost")
        self.assertGreater(sum_power(candidate, "charge", range(0, 24)), 0.0)
        self.assertGreater(sum_power(candidate, "discharge", range(48, 72)), 0.0)
        self.assertLessEqual(
            abs(candidate.metrics.terminal_soc_pct - request.capability.initial_soc_pct),
            request.constraints.terminal_soc_tolerance_pct + 1e-6,
        )

    def test_pv_profile_stores_midday_surplus_before_export(self):
        request = make_midday_pv_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "pv")
        self.assertGreater(sum_power(candidate, "charge", range(40, 56)), 0.0)
        self.assertLess(
            candidate.metrics.grid_export_energy_kwh
            + candidate.metrics.pv_unabsorbed_energy_kwh,
            no_storage_unused_pv_energy(request),
        )

    def test_demand_profile_reports_unavoidable_exceedance(self):
        request = make_demand_peak_request(max_discharge_kw=20.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "balanced")
        self.assertGreater(candidate.metrics.peak_demand_exceed_kw, 0.0)
        self.assertLessEqual(
            max(point.target_power_kw for point in candidate.plan), 20.0 + 1e-6
        )

    def test_disabled_storage_returns_an_idle_plan(self):
        result = M4Optimizer(model_version="m4-milp-v1").optimize(
            make_request(available=False)
        )
        for candidate in result.candidates:
            self.assertTrue(all(point.mode == "idle" for point in candidate.plan))

    def test_station_requests_do_not_share_identity_or_results(self):
        optimizer = M4Optimizer(model_version="m4-milp-v1")
        station_1 = optimizer.optimize(
            make_request(station_id="station-1", load_kw=100.0)
        )
        station_2 = optimizer.optimize(
            make_request(station_id="station-2", load_kw=300.0)
        )
        self.assertEqual(station_1.station_id, "station-1")
        self.assertEqual(station_2.station_id, "station-2")
        self.assertNotEqual(
            candidate_by_id(station_1, "balanced").metrics.max_grid_import_kw,
            candidate_by_id(station_2, "balanced").metrics.max_grid_import_kw,
        )

    def test_sufficient_battery_power_eliminates_demand_exceedance(self):
        request = make_demand_peak_request(max_discharge_kw=200.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "balanced")
        self.assertLessEqual(candidate.metrics.peak_demand_exceed_kw, 1e-6)

    def test_disabled_export_reports_unabsorbed_pv_without_export_command(self):
        request = make_midday_pv_request(
            grid_export_enabled=False,
            grid_export_limit_kw=0.0,
        )
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        candidate = candidate_by_id(result, "pv")
        self.assertIn("PV_UNABSORBED", candidate.risk_codes)
        self.assertTrue(all(point.grid_export_kw <= 1e-7 for point in candidate.plan))

    def test_zero_sell_price_never_reports_export_revenue(self):
        request = make_midday_pv_request(sell_price_per_kwh=0.0)
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        for candidate in result.candidates:
            self.assertEqual(candidate.metrics.export_revenue, 0.0)

    def test_final_solution_respects_every_recorded_objective_lock(self):
        request = make_price_arbitrage_request()
        result = M4Optimizer(model_version="m4-milp-v1").optimize(request)
        profiles = {profile.profile_id: profile for profile in request.profiles}
        for candidate in result.candidates:
            profile = profiles[candidate.profile_id]
            for layer, record in zip(
                profile.objective_order,
                candidate.layers,
                strict=True,
            ):
                final_value = metric_objective_value(candidate.metrics, layer)
                self.assertLessEqual(
                    final_value,
                    record.best_value + record.lock_tolerance + 1e-6,
                )
