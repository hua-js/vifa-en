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
    early_valley_optimal: bool | None = None


def build_layer_objective(
    built: BuiltModel, layer: ObjectiveLayer
) -> NDArray[np.float64]:
    objective = np.zeros(built.index.size, dtype=float)
    for name, weight in layer.terms.items():
        objective += weight * built.objectives[name]
    return objective


def _valley_reordering_locks(
    built: BuiltModel, incumbent: NDArray[np.float64],
) -> list[ObjectiveLock]:
    """Only move existing valley charge; preserve discharge and other periods."""
    locks: list[ObjectiveLock] = []

    def fix(columns: list[int]) -> None:
        vector = np.zeros(built.index.size, dtype=float)
        vector[columns] = 1.0
        value = float(vector @ incumbent)
        locks.extend((ObjectiveLock(vector, value), ObjectiveLock(-vector, -value)))

    eligible = {t for window in built.valley_charge_windows for t in window}
    for column in range(built.index.discharge.start, built.index.discharge.stop):
        fix([column])
    for t, column in enumerate(range(built.index.charge.start, built.index.charge.stop)):
        if t not in eligible:
            fix([column])
    for window in built.valley_charge_windows:
        fix([built.index.charge.start + t for t in window])
    return locks


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
    early_valley_optimal = None

    for layer in profile.objective_order:
        remaining_seconds = time_limit_seconds - (monotonic() - started)
        if remaining_seconds <= 0:
            if incumbent is not None:
                return ProfileSolveResult(
                    status="feasible",
                    x=incumbent,
                    layers=tuple(layer_results),
                    message=(
                        "partial lexicographic result: total time limit exhausted "
                        f"after {len(layer_results)} of "
                        f"{len(profile.objective_order)} layers; returning last incumbent"
                    ),
                    solve_seconds=monotonic() - started,
                )
            return ProfileSolveResult(
                status="timeout",
                x=None,
                layers=tuple(layer_results),
                message="total time limit exhausted before solving the next layer",
                solve_seconds=monotonic() - started,
            )

        objective = build_layer_objective(built, layer)
        early_valley = "valley_charge_delay" in layer.terms
        if early_valley and incumbent is not None:
            locks.extend(_valley_reordering_locks(built, incumbent))
        raw = solve_milp(
            built.problem,
            objective,
            tuple(locks),
            remaining_seconds,
            0.0 if early_valley else mip_rel_gap,
        )
        final_message = raw.message
        if raw.status == "timeout" and raw.x is None and incumbent is not None:
            return ProfileSolveResult(
                status="feasible",
                x=incumbent,
                layers=tuple(layer_results),
                message=(
                    "partial lexicographic result: "
                    f"{raw.message}; returning last incumbent after "
                    f"{len(layer_results)} of {len(profile.objective_order)} layers"
                ),
                solve_seconds=monotonic() - started,
            )
        if raw.status not in {"optimal", "feasible"}:
            return ProfileSolveResult(
                status=raw.status,
                x=None,
                layers=tuple(layer_results),
                message=final_message,
                solve_seconds=monotonic() - started,
            )
        if raw.x is None or not np.isfinite(raw.x).all():
            return ProfileSolveResult(
                status="error",
                x=None,
                layers=tuple(layer_results),
                message=f"{raw.message}; solver returned no finite incumbent",
                solve_seconds=monotonic() - started,
            )

        incumbent = raw.x.copy()
        if early_valley:
            early_valley_optimal = raw.status == "optimal"
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
        early_valley_optimal=early_valley_optimal,
    )
