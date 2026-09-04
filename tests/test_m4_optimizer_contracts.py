import unittest
from datetime import timedelta

from pydantic import ValidationError

from tests.m4_optimizer_test_support import make_request


class M4OptimizerContractTests(unittest.TestCase):
    def test_request_requires_exactly_96_contiguous_points(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"] = payload["points"][:-1]
        with self.assertRaisesRegex(ValidationError, "96 points"):
            type(request).model_validate(payload)

    def test_request_rejects_a_shifted_timestamp(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"][12]["timestamp"] += timedelta(minutes=1)
        with self.assertRaisesRegex(ValidationError, "15-minute timeline"):
            type(request).model_validate(payload)

    def test_request_rejects_non_finite_forecast(self):
        request = make_request()
        payload = request.model_dump()
        payload["points"][2]["pv_forecast_kw"] = float("nan")
        with self.assertRaises(ValidationError):
            type(request).model_validate(payload)

    def test_request_requires_balanced_cost_and_pv_profiles_once_each(self):
        request = make_request()
        payload = request.model_dump()
        payload["profiles"] = payload["profiles"][:2]
        with self.assertRaisesRegex(ValidationError, "balanced, cost and pv"):
            type(request).model_validate(payload)

    def test_every_profile_keeps_demand_as_the_first_soft_goal(self):
        request = make_request()
        payload = request.model_dump()
        payload["profiles"][0]["objective_order"][0]["terms"] = {"energy_cost": 1.0}
        with self.assertRaisesRegex(ValidationError, "demand objectives"):
            type(request).model_validate(payload)

    def test_initial_soc_must_be_inside_absolute_bounds(self):
        request = make_request()
        payload = request.model_dump()
        payload["capability"]["initial_soc_pct"] = 5.0
        payload["constraints"]["soc_min_pct"] = 10.0
        with self.assertRaisesRegex(ValidationError, "initial SOC"):
            type(request).model_validate(payload)

    def test_stale_ems_snapshot_is_rejected(self):
        request = make_request()
        payload = request.model_dump()
        payload["input_observed_at"] = request.plan_start_at - timedelta(minutes=31)
        payload["max_input_age_seconds"] = 1800
        with self.assertRaisesRegex(ValidationError, "stale"):
            type(request).model_validate(payload)
