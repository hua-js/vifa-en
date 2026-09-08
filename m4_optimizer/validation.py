"""Independent validation for public optimization candidates."""

from m4_optimizer.contracts import CandidateMetrics, CandidateResult, OptimizationRequest
from m4_optimizer.metrics import INTERVAL_HOURS, calculate_metrics


class ResultValidationError(ValueError):
    """Raised when a candidate cannot be reproduced from its public plan."""


def validate_candidate(
    request: OptimizationRequest,
    candidate: CandidateResult,
    tolerance: float = 1e-6,
) -> None:
    """Validate all public feasibility rules and independently recalculated metrics."""
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative")
    if candidate.status not in {"optimal", "feasible"}:
        if candidate.plan or candidate.metrics is not None:
            raise ResultValidationError(
                "non-success candidate must have an empty plan and no metrics"
            )
        return
    if len(candidate.plan) != len(request.points):
        raise ResultValidationError("plan length must match request points")
    if candidate.metrics is None:
        raise ResultValidationError("successful candidate must include metrics")

    capability = request.capability
    constraints = request.constraints
    current_energy = capability.energy_capacity_kwh * capability.initial_soc_pct / 100.0
    for index, (source, point) in enumerate(zip(request.points, candidate.plan, strict=True)):
        label = f"point {index} ({source.timestamp.isoformat()})"
        if point.timestamp != source.timestamp:
            _raise(label, "timestamp")
        charge_kw, discharge_kw = _mode_power(point.mode, point.target_power_kw, label, tolerance)
        if not capability.available and (charge_kw > tolerance or discharge_kw > tolerance):
            _raise(label, "device availability")
        if charge_kw - capability.max_charge_kw > tolerance:
            _raise(label, "charge power limit")
        if discharge_kw - capability.max_discharge_kw > tolerance:
            _raise(label, "discharge power limit")
        if (
            constraints.grid_import_limit_kw is not None
            and point.grid_import_kw - constraints.grid_import_limit_kw > tolerance
        ):
            _raise(label, "grid import limit")
        if point.grid_import_kw > tolerance and point.grid_export_kw > tolerance:
            _raise(label, "import and export are mutually exclusive")
        if not constraints.grid_export_enabled and point.grid_export_kw > tolerance:
            _raise(label, "grid export disabled")
        if (
            constraints.grid_export_enabled
            and point.grid_export_kw - constraints.grid_export_limit_kw > tolerance
        ):
            _raise(label, "grid export limit")
        if point.pv_unabsorbed_kw - source.pv_forecast_kw > tolerance:
            _raise(label, "PV availability")
        if (
            point.grid_export_kw
            + point.pv_unabsorbed_kw
            - source.pv_forecast_kw
            > tolerance
        ):
            _raise(label, "PV attribution boundary")

        if request.pv_dispatch_policy == "load_first_economic":
            surplus = max(source.pv_forecast_kw - source.load_forecast_kw, 0.0)
            if (discharge_kw - max(source.load_forecast_kw - source.pv_forecast_kw, 0.0) > tolerance
                    or point.grid_export_kw + point.pv_unabsorbed_kw - surplus > tolerance):
                _raise(label, "PV load priority")
            if surplus > 0.0:
                if point.grid_import_kw > tolerance:
                    _raise(label, "PV surplus grid import")
                available_charge = capability.max_charge_kw if capability.available else 0.0
                headroom_kw = max(
                    capability.energy_capacity_kwh * constraints.soc_max_pct / 100.0
                    - current_energy, 0.0,
                ) / (capability.charge_efficiency * INTERVAL_HOURS)
                absorbable = min(surplus, available_charge, headroom_kw)
                if not constraints.grid_export_enabled or point.pv_unabsorbed_kw > tolerance:
                    if abs(charge_kw - absorbable) > tolerance:
                        _raise(label, "PV surplus absorption")
                if point.pv_unabsorbed_kw > tolerance:
                    export_limit = min(surplus, constraints.grid_export_limit_kw) if constraints.grid_export_enabled else 0.0
                    if abs(point.grid_export_kw - export_limit) > tolerance:
                        _raise(label, "PV curtailment before export limit")

        balance_error = (
            source.pv_forecast_kw
            + point.grid_import_kw
            + discharge_kw
            - source.load_forecast_kw
            - charge_kw
            - point.grid_export_kw
            - point.pv_unabsorbed_kw
        )
        if abs(balance_error) > tolerance:
            _raise(label, "power balance")
        expected_demand_exceed = max(
            point.grid_import_kw - constraints.demand_limit_kw, 0.0
        )
        if abs(point.demand_exceed_kw - expected_demand_exceed) > tolerance:
            _raise(label, "demand exceed")

        expected_energy = (
            current_energy
            + capability.charge_efficiency * charge_kw * INTERVAL_HOURS
            - discharge_kw * INTERVAL_HOURS / capability.discharge_efficiency
        )
        expected_soc_pct = expected_energy / capability.energy_capacity_kwh * 100.0
        if abs(point.expected_soc_pct - expected_soc_pct) > tolerance:
            _raise(label, "SOC state")
        if expected_soc_pct < constraints.soc_min_pct - tolerance:
            _raise(label, "SOC minimum")
        if expected_soc_pct > constraints.soc_max_pct + tolerance:
            _raise(label, "SOC maximum")
        current_energy = expected_energy

    terminal_soc_pct = current_energy / capability.energy_capacity_kwh * 100.0
    if abs(terminal_soc_pct - capability.initial_soc_pct) > (
        constraints.terminal_soc_tolerance_pct + tolerance
    ):
        raise ResultValidationError("terminal SOC boundary")

    recalculated = calculate_metrics(request, candidate.plan)
    _validate_metrics(candidate.metrics, recalculated, tolerance)


def _mode_power(
    mode: str, target_power_kw: float, label: str, tolerance: float
) -> tuple[float, float]:
    if mode == "charge":
        return target_power_kw, 0.0
    if mode == "discharge":
        return 0.0, target_power_kw
    if target_power_kw > tolerance:
        _raise(label, "idle power")
    return 0.0, 0.0


def _validate_metrics(
    supplied: CandidateMetrics, recalculated: CandidateMetrics, tolerance: float
) -> None:
    for name in CandidateMetrics.model_fields:
        supplied_value = getattr(supplied, name)
        expected_value = getattr(recalculated, name)
        if abs(supplied_value - expected_value) > tolerance:
            raise ResultValidationError(
                f"metric {name} does not match public-plan recomputation"
            )


def _raise(label: str, rule: str) -> None:
    raise ResultValidationError(f"{label}: {rule}")
