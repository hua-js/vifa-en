"""Public three-candidate optimization service."""

from datetime import datetime, timezone
from importlib.metadata import version

from m4_optimizer.contracts import CandidateResult, OptimizationRequest, OptimizationResult
from m4_optimizer.lexicographic import solve_profile
from m4_optimizer.metrics import calculate_metrics, materialize_plan
from m4_optimizer.model import build_model
from m4_optimizer.profiles import ordered_profiles
from m4_optimizer.validation import validate_candidate


RISK_MESSAGES = {
    "EARLY_VALLEY_PREFERENCE_INCOMPLETE": (
        "候选保留可行计划，凌晨谷段早充偏好尚未确认最优。"
    ),
    "PV_UNABSORBED": (
        "存在未吸收光伏余量；该值仅用于风险提示，不是光伏限发指令。"
    ),
    "CANDIDATE_PROCESSING_ERROR": (
        "候选在计划解码、指标复算或独立验证阶段失败，不能作为可用计划。"
    ),
}


class M4Optimizer:
    """Build and solve all request-defined candidate profiles for one station."""

    def __init__(self, *, model_version: str) -> None:
        if not model_version.strip():
            raise ValueError("model_version is required")
        self.model_version = model_version

    def optimize(self, request: OptimizationRequest) -> OptimizationResult:
        """Return independently validated candidates in stable display order."""
        started_at = datetime.now(timezone.utc)
        built = build_model(request)
        candidates: list[CandidateResult] = []

        for profile in ordered_profiles(request):
            plan_version = self._plan_version(
                request,
                profile.profile_id,
                profile.profile_version,
            )
            solved = solve_profile(
                built,
                profile,
                request.solver_time_limit_seconds,
                request.solver_mip_rel_gap,
            )
            stage = "candidate_result"
            try:
                if solved.x is None:
                    candidate = CandidateResult(
                        profile_id=profile.profile_id,
                        profile_version=profile.profile_version,
                        plan_version=plan_version,
                        status=solved.status,
                        solver_message=solved.message,
                        solve_seconds=solved.solve_seconds,
                        plan=[],
                        metrics=None,
                        layers=list(solved.layers),
                        risk_codes=[],
                        risk_messages=[],
                    )
                else:
                    stage = "materialize_plan"
                    plan = materialize_plan(request, built, solved.x)
                    stage = "calculate_metrics"
                    metrics = calculate_metrics(request, plan)
                    risk_codes = []
                    if metrics.pv_unabsorbed_energy_kwh > 1e-6:
                        risk_codes.append("PV_UNABSORBED")
                    if (any("valley_charge_delay" in item.terms
                            for item in profile.objective_order)
                            and solved.early_valley_optimal is not True):
                        risk_codes.append("EARLY_VALLEY_PREFERENCE_INCOMPLETE")
                    stage = "candidate_result"
                    candidate = CandidateResult(
                        profile_id=profile.profile_id,
                        profile_version=profile.profile_version,
                        plan_version=plan_version,
                        status=solved.status,
                        solver_message=solved.message,
                        solve_seconds=solved.solve_seconds,
                        plan=plan,
                        metrics=metrics,
                        layers=list(solved.layers),
                        risk_codes=risk_codes,
                        risk_messages=[RISK_MESSAGES[code] for code in risk_codes],
                    )
                stage = "validate_candidate"
                validate_candidate(request, candidate)
            except ValueError as exc:
                error_message = (
                    f"{solved.message}; {stage} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                candidate = CandidateResult(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    plan_version=plan_version,
                    status="error",
                    solver_message=error_message,
                    solve_seconds=solved.solve_seconds,
                    plan=[],
                    metrics=None,
                    layers=list(solved.layers),
                    risk_codes=["CANDIDATE_PROCESSING_ERROR"],
                    risk_messages=[RISK_MESSAGES["CANDIDATE_PROCESSING_ERROR"]],
                )
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

    def _plan_version(
        self,
        request: OptimizationRequest,
        profile_id: str,
        profile_version: str,
    ) -> str:
        return (
            f"{request.request_id}/{self.model_version}/"
            f"{profile_id}/{profile_version}"
        )
