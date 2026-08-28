from dataclasses import dataclass
import math
from collections.abc import Sequence
from statistics import median


@dataclass(frozen=True)
class MetricResult:
    expected_count: int
    valid_count: int
    zero_actual_count: int
    mape_percent: float | None
    mae: float | None
    smape_percent: float | None
    outcome: str
    wape_percent: float | None = None
    median_ape_percent: float | None = None
    p90_ape_percent: float | None = None


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _finite_pair(actual: object, forecast: object, quality: object) -> tuple[float, float] | None:
    if quality != "valid" or actual is None or forecast is None:
        return None
    try:
        actual_value = float(actual)
        forecast_value = float(forecast)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(actual_value) or not math.isfinite(forecast_value):
        return None
    return actual_value, forecast_value


def evaluate_series(
    actual: Sequence[object],
    forecast: Sequence[object],
    quality: Sequence[object],
    minimum_valid: int = 605,
) -> MetricResult:
    if not (len(actual) == len(forecast) == len(quality)):
        raise ValueError("metric inputs must have equal length")

    valid_pairs = [
        pair
        for pair in (
            _finite_pair(a, f, q)
            for a, f, q in zip(actual, forecast, quality, strict=True)
        )
        if pair is not None
    ]
    zero_actual_count = sum(a == 0 for a, _ in valid_pairs)
    scored = [(a, f) for a, f in valid_pairs if a != 0]
    if not scored:
        return MetricResult(
            expected_count=len(actual),
            valid_count=0,
            zero_actual_count=zero_actual_count,
            mape_percent=None,
            mae=None,
            smape_percent=None,
            outcome="insufficient_data",
            wape_percent=None,
            median_ape_percent=None,
            p90_ape_percent=None,
        )

    ape = [abs(a - f) / abs(a) for a, f in scored]
    absolute_errors = [abs(a - f) for a, f in scored]
    smape = [
        200 * abs(a - f) / (abs(a) + abs(f))
        for a, f in scored
        if abs(a) + abs(f) > 0
    ]
    mape_percent = 100 * sum(ape) / len(ape)
    outcome = (
        "insufficient_data"
        if len(scored) < minimum_valid
        else "passed"
        if mape_percent <= 30
        else "failed"
    )
    return MetricResult(
        expected_count=len(actual),
        valid_count=len(scored),
        zero_actual_count=zero_actual_count,
        mape_percent=mape_percent,
        mae=sum(absolute_errors) / len(absolute_errors),
        smape_percent=sum(smape) / len(smape),
        outcome=outcome,
        wape_percent=(
            100 * sum(absolute_errors) / sum(abs(a) for a, _ in scored)
        ),
        median_ape_percent=100 * median(ape),
        p90_ape_percent=100 * _percentile(ape, 0.9),
    )


def overall_outcome(results: list[MetricResult]) -> str:
    if any(result.outcome == "insufficient_data" for result in results):
        return "insufficient_data"
    return "failed" if any(result.outcome == "failed" for result in results) else "passed"
