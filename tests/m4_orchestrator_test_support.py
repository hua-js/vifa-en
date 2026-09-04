from datetime import datetime, timedelta, timezone

from m4_optimizer.contracts import OptimizationResult

from tests.m4_optimizer_test_support import make_request


UTC = timezone.utc
FIXED_STARTED_AT = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
FIXED_FINISHED_AT = FIXED_STARTED_AT + timedelta(seconds=1)


def make_optimization_result(**overrides: object) -> OptimizationResult:
    request = make_request()
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
        "candidates": [],
    }
    values.update(overrides)
    return OptimizationResult(**values)
