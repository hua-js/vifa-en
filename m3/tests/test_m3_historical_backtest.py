"""Historical rolling-backtest tests for the local M3 dashboard."""

from datetime import datetime, timedelta
import unittest

from m3.worker.contracts import ForecastPoint, ForecastSeries, ObservationPoint
from m3.worker.services.live_dashboard_service import build_historical_backtest
from m3.worker.domain.forecasting import seasonal_naive_champion


AS_OF = datetime.fromisoformat("2026-08-26T00:00:00+08:00")
SERIES = (
    ("station_total_load", "kW", 100.0),
    ("storage_soc", "%", 50.0),
)


def observations(*, load_in_backtest: float = 100.0) -> list[ObservationPoint]:
    start = AS_OF - timedelta(days=15)
    output: list[ObservationPoint] = []
    for index in range(15 * 96):
        data_time = start + timedelta(minutes=15 * index)
        in_backtest = data_time >= AS_OF - timedelta(days=7)
        for unique_id, _, normal_value in SERIES:
            value = (
                load_in_backtest
                if unique_id == "station_total_load" and in_backtest
                else normal_value
            )
            output.append(ObservationPoint(
                unique_id=unique_id,
                ds=data_time,
                y=value,
                quality="valid",
                source_revision=index,
            ))
    return output


def constant_forecaster(calls: list[datetime]):
    def forecast(training: list[ObservationPoint], cutoff: datetime):
        calls.append(cutoff)
        if training:
            assert max(point.ds for point in training) < cutoff
        output: list[ForecastSeries] = []
        for unique_id, unit, value in SERIES:
            points = []
            for index in range(96):
                data_time = cutoff + timedelta(minutes=15 * index)
                points.append(ForecastPoint(
                    data_time=data_time,
                    target_time=data_time + timedelta(minutes=15),
                    horizon_step=index + 1,
                    raw_forecast=value,
                    forecast_value=value,
                    is_clipped=False,
                ))
            output.append(ForecastSeries(
                unique_id=unique_id,
                unit=unit,
                model_name="SeasonalNaive",
                status="warming_up",
                points=points,
            ))
        return output

    return forecast


class HistoricalBacktestTests(unittest.TestCase):
    def test_default_backtest_selects_two_champions_before_the_holdout_window(self):
        """Daily refits may use later history, but model selection must run only before the seven-day holdout."""
        selection_ends: list[datetime] = []

        def selector(dataset):
            selection_ends.append(dataset.end)
            return seasonal_naive_champion(dataset)

        result = build_historical_backtest(
            observations(), AS_OF, champion_selector=selector
        )

        self.assertEqual(len(selection_ends), 2)
        self.assertTrue(all(
            training_end < AS_OF - timedelta(days=7)
            for training_end in selection_ends
        ))
        self.assertEqual(result.status, "passed")

    def test_seven_daily_windows_never_train_on_their_future(self):
        """Including any test-window point in training would leak future actuals."""
        calls: list[datetime] = []

        result = build_historical_backtest(
            observations(), AS_OF, forecast_at=constant_forecaster(calls)
        )

        self.assertEqual(
            calls,
            [AS_OF - timedelta(days=offset) for offset in range(7, 0, -1)],
        )
        self.assertEqual(result.acceptance_run_id, "historical-backtest-20260826-0000")
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.completed_days, 7)
        self.assertEqual(
            [
                (item.unique_id, item.valid_count, item.mape_percent, item.outcome)
                for item in result.results
            ],
            [
                ("station_total_load", 672, 0.0, "passed"),
                ("storage_soc", 672, 0.0, "passed"),
            ],
        )

    def test_valid_outlier_periods_are_scored_when_no_exclusion_data_exists(self):
        """A valid load regime change must fail accuracy instead of being silently excluded."""
        result = build_historical_backtest(
            observations(load_in_backtest=200.0),
            AS_OF,
            forecast_at=constant_forecaster([]),
        )

        load, soc = result.results
        self.assertEqual(
            (load.valid_count, load.mape_percent, load.wape_percent, load.outcome),
            (672, 50.0, 50.0, "failed"),
        )
        self.assertEqual(soc.outcome, "passed")
        self.assertEqual(result.status, "failed")


if __name__ == "__main__":
    unittest.main()
