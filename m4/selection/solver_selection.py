"""Pyomo/HiGHS decisions over independently validated dispatch candidates.

Each preference layer is a one-of-N binary model. Exact decimal values are
encoded as small ordinal costs, preserving ordering without loss from floating
point scaling. Tolerances are then applied to the original exact values before
solving the next layer. The old deterministic engine remains the audit oracle.
"""
from fractions import Fraction

import numpy as np
import pyomo.environ as pyo

from m4.optimizer.contracts import OptimizationRequest, OptimizationResult
from m4.optimizer.solver import PyomoProblem, solve_milp as solve_pyomo_model
from m4.selection.contracts import SelectionPolicy, SelectionResult
from m4.selection.service import _select_candidate, select_candidate


def _solve_minimum(values: dict[str, Fraction]) -> str:
    if not values:
        raise ValueError('no candidate is available to the decision solver')
    ids = tuple(values)
    order = {value: rank for rank, value in enumerate(sorted(set(values.values())))}
    costs = np.array([order[values[pid]] for pid in ids], dtype=float)
    model = pyo.ConcreteModel(name='m4_candidate_decision')
    model.profiles = pyo.Set(initialize=ids, ordered=True)
    model.choose = pyo.Var(model.profiles, domain=pyo.Binary)
    model.exactly_one = pyo.Constraint(expr=sum(model.choose[pid] for pid in ids) == 1)
    raw = solve_pyomo_model(
        PyomoProblem(model, tuple(model.choose[pid] for pid in ids)),
        costs, (), 5.0, 0.0,
    )
    if raw.status != 'optimal' or raw.x is None:
        raise ValueError('candidate decision solver did not prove an optimal choice')
    x = np.asarray(raw.x)
    if (x.shape != costs.shape or not np.isfinite(x).all()
            or not np.allclose(x, np.round(x), atol=1e-7, rtol=0)
            or np.any(x < -1e-7) or np.any(x > 1 + 1e-7)
            or abs(float(x.sum()) - 1.0) > 1e-7):
        raise ValueError('candidate decision solver returned an invalid one-of-N choice')
    chosen = ids[int(np.argmax(x))]
    # Validate the tiny decision model independently of backend status/rounding.
    if any(value < values[chosen] for value in values.values()):
        raise ValueError('candidate decision failed independent minimum validation')
    return chosen


def select_candidate_with_solver(
    request: OptimizationRequest,
    result: OptimizationResult,
    policy: SelectionPolicy | None = None,
) -> SelectionResult:
    answer = _select_candidate(request, result, policy, minimum_selector=_solve_minimum)
    reference = select_candidate(request, result, policy)
    if answer != reference:
        raise ValueError('solver decision disagrees with independent policy validation')
    return SelectionResult.model_validate({
        **answer.model_dump(), 'selector_version': 'pyomo-highs-selection-v2',
    })
