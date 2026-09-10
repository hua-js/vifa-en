"""Pyomo / HiGHS adapter, independent of station dispatch rules."""
from dataclasses import dataclass
from threading import Lock
from time import monotonic

import numpy as np
import pyomo.environ as pyo
from numpy.typing import NDArray
from pyomo.contrib.appsi.base import TerminationCondition
from pyomo.contrib.appsi.solvers import Highs
from pyomo.core.base.var import VarData
from scipy.sparse import csr_matrix

from m4.optimizer.contracts import CandidateStatus


FloatArray = NDArray[np.float64]
MIP_FEASIBILITY_TOLERANCE = 1e-9
PROVEN_OPTIMAL_MIP_GAP_TOLERANCE = 1e-9
# APPSI captures process-wide file descriptors while talking to HiGHS. Keep
# that section serialized; models, solver instances and incumbents remain local.
_HIGHS_LOCK = Lock()


@dataclass(frozen=True)
class MilpProblem:
    """Compatibility input for small generic matrix-based callers/tests only."""
    integrality: NDArray[np.uint8]
    lower_bounds: FloatArray
    upper_bounds: FloatArray
    matrix: csr_matrix
    constraint_lower: FloatArray
    constraint_upper: FloatArray


@dataclass(frozen=True)
class PyomoProblem:
    model: pyo.ConcreteModel
    variables: tuple[VarData, ...]

    @property
    def lower_bounds(self) -> FloatArray:
        return np.array([v.lb if v.lb is not None else -np.inf for v in self.variables])

    @property
    def upper_bounds(self) -> FloatArray:
        return np.array([v.ub if v.ub is not None else np.inf for v in self.variables])


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


def _linear_expression(variables: tuple[VarData, ...], vector: FloatArray):
    return pyo.quicksum(float(vector[i]) * variables[i] for i in np.flatnonzero(vector))


def _matrix_model(problem: MilpProblem) -> PyomoProblem:
    """Translate the compatibility format; the station builder never uses this."""
    model = pyo.ConcreteModel(name="generic_milp")
    model.columns = pyo.RangeSet(0, len(problem.integrality) - 1)
    if np.any(~np.isin(problem.integrality, (0, 1))):
        raise ValueError("only continuous and integer compatibility variables are supported")
    model.x = pyo.Var(
        model.columns,
        domain=lambda m, i: pyo.Integers if problem.integrality[i] else pyo.Reals,
        bounds=lambda m, i: (
            None if np.isneginf(problem.lower_bounds[i]) else float(problem.lower_bounds[i]),
            None if np.isposinf(problem.upper_bounds[i]) else float(problem.upper_bounds[i]),
        ),
    )
    variables = tuple(model.x.values())
    model.linear_constraints = pyo.ConstraintList()
    for i in range(problem.matrix.shape[0]):
        row = problem.matrix.getrow(i)
        expr = pyo.quicksum(float(value) * variables[column]
                           for column, value in zip(row.indices, row.data))
        lower, upper = problem.constraint_lower[i], problem.constraint_upper[i]
        model.linear_constraints.add((None if np.isneginf(lower) else float(lower),
                                      expr, None if np.isposinf(upper) else float(upper)))
    return PyomoProblem(model, variables)


def _finite(value) -> bool:
    return value is not None and bool(np.isfinite(value))


def _map_result(backend: Highs, result, variables, objective: FloatArray) -> RawSolveResult:
    termination = result.termination_condition
    allowed_incumbent = termination in {
        TerminationCondition.optimal, TerminationCondition.maxTimeLimit,
        TerminationCondition.maxIterations, TerminationCondition.objectiveLimit,
    }
    x = None
    if allowed_incumbent and _finite(result.best_feasible_objective):
        try:
            # Read the current solver's result, never values left on a Pyomo model.
            primals = backend.get_primals(variables)
            values = np.array([primals[v] for v in variables], dtype=float)
            if np.isfinite(values).all():
                x = values
        except (RuntimeError, KeyError, ValueError):
            pass

    mip_gap = None
    primal, bound = result.best_feasible_objective, result.best_objective_bound
    if x is not None and _finite(primal) and _finite(bound):
        difference = abs(float(primal) - float(bound))
        mip_gap = difference / abs(float(primal)) if primal != 0 else (0.0 if difference == 0 else np.inf)
    proven = mip_gap is not None and mip_gap <= PROVEN_OPTIMAL_MIP_GAP_TOLERANCE
    if termination == TerminationCondition.optimal:
        status: CandidateStatus = "error" if x is None else ("optimal" if proven else "feasible")
    elif termination == TerminationCondition.maxTimeLimit:
        status = "feasible" if x is not None else "timeout"
    elif termination in {TerminationCondition.maxIterations, TerminationCondition.objectiveLimit} and x is not None:
        status = "feasible"
    elif termination == TerminationCondition.infeasible:
        status = "infeasible"
    else:
        status = "error"
    return RawSolveResult(status=status, x=x,
                          objective_value=float(objective @ x) if x is not None else None,
                          message=f"HiGHS: {termination.name}", mip_gap=mip_gap)


def solve_milp(
    problem: PyomoProblem | MilpProblem,
    objective: FloatArray,
    locks: tuple[ObjectiveLock, ...],
    time_limit_seconds: float,
    mip_rel_gap: float,
) -> RawSolveResult:
    """Solve an isolated native model, retaining the established vector result API."""
    started = monotonic()
    variable_count = len(problem.variables) if isinstance(problem, PyomoProblem) else len(problem.integrality)
    if np.shape(objective) != (variable_count,):
        raise ValueError("objective length must match the number of variables")
    for lock in locks:
        if np.shape(lock.vector) != (variable_count,):
            raise ValueError("objective lock length must match the number of variables")

    def timeout():
        return RawSolveResult("timeout", None, None, "time limit exhausted before HiGHS solve", None)

    if time_limit_seconds <= 0:
        return timeout()
    native = problem if isinstance(problem, PyomoProblem) else _matrix_model(problem)
    # A caller can reuse a built model across profiles or concurrent requests.
    # Neither objective locks nor incumbent values leak back into that model.
    model = native.model.clone()
    variables = tuple(model.find_component(var.name) for var in native.variables)
    model.active_objective = pyo.Objective(expr=_linear_expression(variables, objective))
    model.objective_locks = pyo.ConstraintList()
    for lock in locks:
        model.objective_locks.add((None, _linear_expression(variables, lock.vector), float(lock.upper_bound)))

    remaining = time_limit_seconds - (monotonic() - started)
    if remaining <= 0 or not _HIGHS_LOCK.acquire(timeout=remaining):
        return timeout()
    try:
        remaining = time_limit_seconds - (monotonic() - started)
        if remaining <= 0:
            return timeout()
        backend = Highs(only_child_vars=True)
        backend.config.load_solution = False
        backend.config.mip_gap = mip_rel_gap
        backend.highs_options.update({
            "mip_feasibility_tolerance": MIP_FEASIBILITY_TOLERANCE,
            "presolve": "on",
            "threads": 1,
        })
        backend.set_instance(model)
        remaining = time_limit_seconds - (monotonic() - started)
        if remaining <= 0:
            return timeout()
        backend.config.time_limit = remaining
        result = backend.solve(model)
        return _map_result(backend, result, variables, objective)
    finally:
        _HIGHS_LOCK.release()
