import unittest
from datetime import timedelta, timezone, tzinfo

from pydantic import ValidationError

from m4.tests.m4_optimizer_test_support import make_candidate_from_optimizer, make_request


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

    def test_request_requires_a_valid_utc_offset_on_every_timestamp(self):
        request = make_request()
        invalid_timezone = MissingOffsetTimezone()
        cases = ("plan_start_at", "input_observed_at", "point")

        for case in cases:
            with self.subTest(case=case):
                payload = request.model_dump()
                if case == "point":
                    payload["points"][3]["timestamp"] = payload["points"][3][
                        "timestamp"
                    ].replace(tzinfo=invalid_timezone)
                else:
                    payload[case] = payload[case].replace(tzinfo=invalid_timezone)
                with self.assertRaisesRegex(ValidationError, "valid UTC offset"):
                    type(request).model_validate(payload)

    def test_request_rejects_mixed_utc_offsets_even_for_the_same_instants(self):
        request = make_request()

        for location in ("input_observed_at", "point"):
            with self.subTest(location=location):
                payload = request.model_dump()
                if location == "point":
                    payload["points"][0]["timestamp"] = payload["points"][0][
                        "timestamp"
                    ].astimezone(timezone.utc)
                else:
                    payload[location] = payload[location].astimezone(timezone.utc)
                with self.assertRaisesRegex(ValidationError, "same UTC offset"):
                    type(request).model_validate(payload)

    def test_request_rejects_empty_or_blank_source_versions(self):
        request = make_request()
        invalid_versions = (
            {},
            {"": "forecast-v1"},
            {"   ": "forecast-v1"},
            {"forecast": ""},
            {"forecast": "   "},
        )

        for source_versions in invalid_versions:
            with self.subTest(source_versions=source_versions):
                payload = request.model_dump()
                payload["source_versions"] = source_versions
                with self.assertRaisesRegex(ValidationError, "source_versions"):
                    type(request).model_validate(payload)

    def test_request_rejects_illegal_capacity_and_efficiency(self):
        request = make_request()
        invalid_capabilities = (
            ("energy_capacity_kwh", 0.0),
            ("energy_capacity_kwh", -1.0),
            ("charge_efficiency", 0.0),
            ("charge_efficiency", 1.01),
            ("discharge_efficiency", 0.0),
            ("discharge_efficiency", 1.01),
        )

        for field, value in invalid_capabilities:
            with self.subTest(field=field, value=value):
                payload = request.model_dump()
                payload["capability"][field] = value
                with self.assertRaises(ValidationError):
                    type(request).model_validate(payload)

    def test_candidate_requires_nonblank_plan_version(self):
        candidate = make_candidate_from_optimizer(make_request())
        payload = candidate.model_dump()
        payload["plan_version"] = "   "
        payload["risk_messages"] = []

        with self.assertRaisesRegex(ValidationError, "plan_version must be non-blank"):
            type(candidate).model_validate(payload)

    def test_candidate_requires_risk_codes_and_messages_to_align(self):
        candidate = make_candidate_from_optimizer(make_request())
        payload = candidate.model_dump()
        payload["plan_version"] = "request-test-001/balanced"
        payload["risk_codes"] = ["PV_UNABSORBED"]
        payload["risk_messages"] = []

        with self.assertRaisesRegex(ValidationError, "same length"):
            type(candidate).model_validate(payload)


class MissingOffsetTimezone(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "missing-offset"
