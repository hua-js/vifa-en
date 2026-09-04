import unittest

from m4_optimizer import M4Optimizer
from tests.m4_optimizer_test_support import make_request


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
