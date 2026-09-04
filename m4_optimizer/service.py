"""Public three-candidate optimization service."""

from datetime import datetime, timezone
from importlib.metadata import version

from m4_optimizer.contracts import CandidateResult, OptimizationRequest, OptimizationResult
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.metrics import calculate_metrics, materialize_plan
from m4_optimizer.model import build_model
from m4_optimizer.profiles import ordered_profiles
from m4_optimizer.validation import validate_candidate


class M4Optimizer:
    """Build and solve all request-defined candidate profiles for one station."""

    def __init__(self, *, model_version: str) -> None:
        if not model_version:
            raise ValueError("model_version is required")
        self.model_version = model_version

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        """Return independently validated candidates in stable display order."""
        started_at = datetime.now(timezone.utc)
        built = build_model(request)
        candidates: list[CandidateResult] = []

        for profile in ordered_profiles(request):
            solved = solve_profile(
                built,
                profile,
                request.solver_time_limit_seconds,
                request.solver_mip_rel_gap,
            )
            if solved.x is None:
                candidate = CandidateResult(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    status=solved.status,
                    solver_message=solved.message,
                    solve_seconds=solved.solve_seconds,
                    plan=[],
                    metrics=None,
                    layers=list(solved.layers),
                    risk_codes=[],
                )
            else:
                plan = materialize_plan(request, built, solved.x)
                metrics = calculate_metrics(request, plan)
                risk_codes = []
                if metrics.pv_unabsorbed_energy_kwh > 1e-6:
                    risk_codes.append("PV_UNABSORBED")
                candidate = CandidateResult(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    status=solved.status,
                    solver_message=solved.message,
                    solve_seconds=solved.solve_seconds,
                    plan=plan,
                    metrics=metrics,
                    layers=list(solved.layers),
                    risk_codes=risk_codes,
                )
            validate_candidate(request, candidate)
            candidates.append(candidate)

        return OptimizationResult(
            request_id=request.request_id,
            station_id=request.station_id,
            plan_start_at=request.plan_start_at,
            input_observed_at=request.input_observed_at,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            model_version=self.model_version,
            solver_name="scipy-highs",
            solver_version=version("scipy"),
            source_versions=request.source_versions,
            candidates=candidates,
        )
