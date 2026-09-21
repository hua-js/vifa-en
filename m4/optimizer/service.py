"""Public three-candidate optimization service."""

from datetime import datetime, timezone
from importlib.metadata import version

from m4.optimizer.contracts import CandidateResult, OptimizationRequest, OptimizationResult, GRID_CHARGING_POLICY
from m4.optimizer.lexicographic import solve_profile
from m4.optimizer.metrics import calculate_metrics, materialize_plan
from m4.optimizer.model import build_model
from m4.optimizer.profiles import ordered_profiles
from m4.optimizer.validation import validate_candidate


RISK_MESSAGES = {
    "EARLY_VALLEY_PREFERENCE_INCOMPLETE": (
        "候选保留可行计划，凌晨谷段早充偏好尚未确认最优。"
    ),
    "PEAK_RESERVE_PREFERENCE_INCOMPLETE": (
        "候选保留可行计划，首个峰段开始前的储能准备目标尚未确认最优。"
    ),
    "PEAK_RESERVE_SHORTFALL": (
        "部分时段保留电量未达到目标，已优先满足安全和期末电量要求。"
    ),
    "PV_UNABSORBED": (
        "存在未吸收光伏余量；该值仅用于风险提示，不是光伏限发指令。"
    ),
    "PV_CURTAILMENT_REQUIRED": (
        "本地消纳与允许的外送能力不足，计划需要限发光伏；尚未下发限发指令。"
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

    def optimize(self, request: OptimizationRequest, *, terminal_soc_target_pct: float | None = None) -> OptimizationResult:
        """Return independently validated candidates in stable display order."""
        started_at = datetime.now(timezone.utc)
        built = build_model(request, terminal_soc_target_pct=terminal_soc_target_pct)
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
                    if request.peak_reserve_policy is not None and request.peak_reserve_policy.version == 'peak-reserve-v4':
                        last_peak_start = max((i for i, p in enumerate(request.points)
                            if p.tariff_period in ('jian', 'feng') and
                            (i == 0 or request.points[i-1].tariff_period not in ('jian', 'feng'))), default=0)
                        states = [request.capability.initial_soc_pct, *(p.expected_soc_pct for p in plan)]
                        if any(soc < request.peak_reserve_policy.terminal_soc_min_pct - 1e-6
                               for soc in states[last_peak_start:]):
                            risk_codes.append('PEAK_RESERVE_SHORTFALL')
                    if metrics.pv_unabsorbed_energy_kwh > 1e-6:
                        risk_codes.append("PV_CURTAILMENT_REQUIRED" if request.pv_dispatch_policy != "legacy" else "PV_UNABSORBED")
                    if (any("valley_charge_delay" in item.terms
                            for item in profile.objective_order)
                            and solved.early_valley_optimal is not True):
                        risk_codes.append("EARLY_VALLEY_PREFERENCE_INCOMPLETE")
                    if (request.peak_reserve_policy is not None
                            and solved.peak_reserve_optimal is not True):
                        risk_codes.append("PEAK_RESERVE_PREFERENCE_INCOMPLETE")
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
                validate_candidate(request, candidate, terminal_soc_target_pct=terminal_soc_target_pct)
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
            model_version=self._effective_model_version(request),
            solver_name="pyomo-highs",
            solver_version=f"HiGHS {version('highspy')}; Pyomo {version('pyomo')}",
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
            f"{request.request_id}/{self._effective_model_version(request)}/"
            f"{profile_id}/{profile_version}"
        )

    def _effective_model_version(self, request: OptimizationRequest) -> str:
        model_version = f"{self.model_version}/pyomo-v1/{GRID_CHARGING_POLICY}"
        if request.pv_midday_economic:
            model_version += "/pv-midday-economic-v1"
        if request.ems_schedule_modes is not None:
            model_version += "/ems-original-directions-v1"
        if request.horizon_points == 95:
            model_version += '/horizon-95-v1'
        if request.peak_reserve_policy is not None:
            model_version += '/' + request.peak_reserve_policy.version
        if request.pv_dispatch_policy == "load_first_economic":
            return f"{model_version}/pv-load-first-economic-v1"
        if request.pv_dispatch_policy == "load_first_export_priority":
            return f"{model_version}/pv-{request.pv_dispatch_policy}-v2"
        if request.pv_dispatch_policy == "load_first_storage_priority":
            return f"{model_version}/pv-{request.pv_dispatch_policy}-v1"
        return model_version
