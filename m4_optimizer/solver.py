import warnings
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import csr_matrix, vstack

from m4_optimizer.contracts import CandidateStatus


FloatArray = NDArray[np.float64]
MIP_FEASIBILITY_TOLERANCE = 1e-9


@dataclass(frozen=True)
class MilpProblem:
    integrality: NDArray[np.uint8]
    lower_bounds: FloatArray
    upper_bounds: FloatArray
    matrix: csr_matrix
    constraint_lower: FloatArray
    constraint_upper: FloatArray


@dataclass(frozen=True)
class ObjectiveLock:
    vector: FloatArray
    upper_bound: float


@dataclass(frozen=True)
class RawSolveResult:
    status: CandidateStatus
    x: FloatArray | None
    objective_value: float | None
    message: str
    mip_gap: float | None


def solve_milp(
    problem: MilpProblem,
    objective: FloatArray,
    locks: tuple[ObjectiveLock, ...],
    time_limit_seconds: float,
    mip_rel_gap: float,
) -> RawSolveResult:
    """Solve a generic linear mixed-integer problem with optional objective locks."""
    variable_count = len(problem.integrality)
    if len(objective) != variable_count:
        raise ValueError("objective length must match the number of variables")

    for lock in locks:
        if len(lock.vector) != variable_count:
            raise ValueError("objective lock length must match the number of variables")

    matrix = problem.matrix
    lower = problem.constraint_lower
    upper = problem.constraint_upper
    if locks:
        lock_matrix = csr_matrix(np.vstack([lock.vector for lock in locks]))
        matrix = vstack([matrix, lock_matrix], format="csr")
        lower = np.concatenate([lower, np.full(len(locks), -np.inf)])
        upper = np.concatenate([upper, np.array([lock.upper_bound for lock in locks])])

    with warnings.catch_warnings():
        # SciPy forwards this supported HiGHS option but warns because it is not
        # part of SciPy's small documented option set.
        warnings.filterwarnings(
            "ignore",
            message=r"Unrecognized options detected:.*mip_feasibility_tolerance",
            category=RuntimeWarning,
        )
        result = milp(
            c=objective,
            integrality=problem.integrality,
            bounds=Bounds(problem.lower_bounds, problem.upper_bounds),
            constraints=LinearConstraint(matrix, lower, upper),
            options={
                "time_limit": time_limit_seconds,
                "mip_rel_gap": mip_rel_gap,
                "mip_feasibility_tolerance": MIP_FEASIBILITY_TOLERANCE,
                "presolve": True,
            },
        )

    raw_x = result.x
    x = raw_x if raw_x is not None and np.isfinite(raw_x).all() else None
    if result.status == 0:
        status: CandidateStatus = "optimal"
    elif result.status == 1 and x is not None:
        status = "feasible"
    elif result.status == 1:
        status = "timeout"
    elif result.status == 2:
        status = "infeasible"
    else:
        status = "error"

    objective_value = float(np.dot(objective, x)) if x is not None else None
    mip_gap = getattr(result, "mip_gap", None)
    if mip_gap is not None:
        mip_gap = float(mip_gap)
    return RawSolveResult(
        status=status,
        x=x,
        objective_value=objective_value,
        message=str(result.message),
        mip_gap=mip_gap,
    )
