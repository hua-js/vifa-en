import math
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence
from unittest import TestCase

from m4_optimizer.contracts import (
    CandidateResult,
    CandidateStatus,
    OptimizationRequest,
    OptimizationResult,
    ProfileId,
)

from tests.m4_optimizer_test_support import make_request


UTC = timezone.utc
FIXED_STARTED_AT = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
FIXED_FINISHED_AT = FIXED_STARTED_AT + timedelta(seconds=1)


def assert_nested_close(
    testcase: TestCase,
    left: object,
    right: object,
    path: str = "root",
) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        testcase.assertEqual(left.keys(), right.keys(), msg=path)
        for key in left:
            assert_nested_close(
                testcase,
                left[key],
                right[key],
                f"{path}.{key}",
            )
        return
    if isinstance(left, list) and isinstance(right, list):
        testcase.assertEqual(len(left), len(right), msg=path)
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            assert_nested_close(
                testcase,
                left_item,
                right_item,
                f"{path}[{index}]",
            )
        return
    if isinstance(left, float) and isinstance(right, float):
        testcase.assertTrue(
            math.isfinite(left),
            msg=f"{path}: left is not finite",
        )
        testcase.assertTrue(
            math.isfinite(right),
            msg=f"{path}: right is not finite",
        )
        testcase.assertTrue(
            math.isclose(left, right, rel_tol=0.0, abs_tol=1e-6),
            msg=f"{path}: {left!r} != {right!r}",
        )
        return
    testcase.assertEqual(left, right, msg=path)


def make_candidate_result(
    status: CandidateStatus = "optimal",
    *,
    profile_id: ProfileId = "balanced",
) -> CandidateResult:
    return CandidateResult(
        profile_id=profile_id,
        profile_version=f"test-{profile_id}-v1",
        plan_version=f"test-plan-{profile_id}-v1",
        status=status,
        solver_message="deterministic test result",
        solve_seconds=0.0,
        plan=[],
        metrics=None,
        layers=[],
        risk_codes=[],
        risk_messages=[],
    )


def make_optimization_result(
    request: OptimizationRequest | None = None,
    *,
    candidate_statuses: Sequence[CandidateStatus] = (),
    **overrides: object,
) -> OptimizationResult:
    request = request or make_request()
    profile_ids: tuple[ProfileId, ...] = ("balanced", "cost", "pv")
    values: dict[str, object] = {
        "request_id": request.request_id,
        "station_id": request.station_id,
        "plan_start_at": request.plan_start_at,
        "input_observed_at": request.input_observed_at,
        "started_at": FIXED_STARTED_AT,
        "finished_at": FIXED_FINISHED_AT,
        "model_version": "test-model-v1",
        "solver_name": "scipy-highs",
        "solver_version": "test-solver-v1",
        "source_versions": request.source_versions,
        "candidates": [
            make_candidate_result(status, profile_id=profile_id)
            for profile_id, status in zip(profile_ids, candidate_statuses)
        ],
    }
    values.update(overrides)
    return OptimizationResult(**values)


class DeterministicOptimizer:
    def __init__(
        self,
        *,
        results_by_station_id: Mapping[str, OptimizationResult] | None = None,
        failures_by_station_id: Mapping[str, RuntimeError] | None = None,
    ) -> None:
        self.results_by_station_id = dict(results_by_station_id or {})
        self.failures_by_station_id = dict(failures_by_station_id or {})
        self.calls: list[str] = []

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        self.calls.append(request.station_id)
        failure = self.failures_by_station_id.get(request.station_id)
        if failure is not None:
            raise failure
        result = self.results_by_station_id.get(request.station_id)
        if result is not None:
            return result
        return make_optimization_result(request, candidate_statuses=("optimal",))
