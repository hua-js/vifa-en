from dataclasses import dataclass
from time import monotonic

import numpy as np
from numpy.typing import NDArray

from m4_optimizer.contracts import (
    CandidateStatus,
    LayerResult,
    ObjectiveLayer,
    ObjectiveProfile,
)
from m4_optimizer.model import BuiltModel
from m4_optimizer.solver import ObjectiveLock, solve_milp


@dataclass(frozen=True)
class ProfileSolveResult:
    status: CandidateStatus
    x: NDArray[np.float64] | None
    layers: tuple[LayerResult, ...]
    message: str
    solve_seconds: float


def build_layer_objective(
    built: BuiltModel, layer: ObjectiveLayer
) -> NDArray[np.float64]:
    objective = np.zeros(built.index.size, dtype=float)
    for name, weight in layer.terms.items():
        objective += weight * built.objectives[name]
    return objective


def solve_profile(
    built: BuiltModel,
    profile: ObjectiveProfile,
    time_limit_seconds: float,
    mip_rel_gap: float,
) -> ProfileSolveResult:
    started = monotonic()
    locks: list[ObjectiveLock] = []
    layer_results: list[LayerResult] = []
    incumbent: NDArray[np.float64] | None = None
    final_message = ""
    used_feasible_incumbent = False

    for layer in profile.objective_order:
        remaining_seconds = time_limit_seconds - (monotonic() - started)
        if remaining_seconds <= 0:
            return ProfileSolveResult(
                status="timeout",
                x=None,
                layers=tuple(layer_results),
                message="total time limit exhausted before solving the next layer",
                solve_seconds=monotonic() - started,
            )

        objective = build_layer_objective(built, layer)
        raw = solve_milp(
            built.problem,
            objective,
            tuple(locks),
            remaining_seconds,
            mip_rel_gap,
        )
        final_message = raw.message
        if (
            raw.status not in {"optimal", "feasible"}
            or raw.x is None
            or not np.isfinite(raw.x).all()
        ):
            return ProfileSolveResult(
                status=raw.status,
                x=None,
                layers=tuple(layer_results),
                message=final_message,
                solve_seconds=monotonic() - started,
            )

        incumbent = raw.x
        if raw.status == "feasible":
            used_feasible_incumbent = True

        best_value = float(objective @ raw.x)
        lock_tolerance = max(
            layer.absolute_tolerance,
            layer.relative_tolerance * abs(best_value),
        )
        layer_results.append(
            LayerResult(
                name=layer.name,
                best_value=best_value,
                lock_tolerance=lock_tolerance,
            )
        )
        locks.append(
            ObjectiveLock(
                vector=objective.copy(),
                upper_bound=best_value + lock_tolerance,
            )
        )

    status: CandidateStatus = "feasible" if used_feasible_incumbent else "optimal"
    return ProfileSolveResult(
        status=status,
        x=incumbent,
        layers=tuple(layer_results),
        message=final_message,
        solve_seconds=monotonic() - started,
    )
