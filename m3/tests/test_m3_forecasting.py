import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import numpy as np
from statsforecast import StatsForecast as RealStatsForecast
from statsforecast.models import AutoARIMA, AutoETS, MSTL, SeasonalNaive

from m3.worker.domain.forecasting import (
    Champion,
    candidate_models,
    clip_value,
    forecast_one,
    forecast_one_safe,
    seasonal_naive_champion,
    select_champion,
)
from m3.worker.domain.training_data import build_training_dataset
from m3.worker.errors import M3Error
from m3.worker.domain.work_schedule import LoadScheduleProfile
from m3.tests.m3_test_support import (
    make_cv_result,
    make_forecast_result,
    make_quarter_hour_points,
)


def _scoring_cv_fixture(engine, frame, *, model_name):
    """Supply restored load predictions to isolate scoring from calendar fitting."""
    return engine.cross_validation(df=frame, h=96, step_size=96, n_windows=7)


class M3ForecastingTests(unittest.TestCase):
    def test_candidate_pool_is_exact(self):
        models = candidate_models()
        self.assertEqual(
            [model.alias for model in models],
            ["SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"],
        )
        self.assertEqual(
            [type(model) for model in models],
            [SeasonalNaive, AutoETS, AutoARIMA, MSTL],
        )
        self.assertEqual([model.season_length for model in models[:3]], [96, 96, 96])
        self.assertEqual(models[3].season_length, [96, 672])
        self.assertIsInstance(models[3].trend_forecaster, AutoARIMA)

    def test_load_and_soc_forecasts_use_their_published_bounds(self):
        self.assertEqual(clip_value("station_total_load", -5), (-5.0, 0.0, True))
        self.assertEqual(clip_value("storage_soc", 1), (1.0, 2.0, True))
        self.assertEqual(clip_value("storage_soc", 2), (2.0, 2.0, False))
        self.assertEqual(clip_value("storage_soc", 99), (99.0, 99.0, False))
        self.assertEqual(clip_value("storage_soc", 100), (100.0, 99.0, True))

    def test_less_than_seven_days_emits_no_forecast_points(self):
        dataset = build_training_dataset(
            make_quarter_hour_points(6), "station_total_load"
        )
        series = forecast_one(dataset, None, dataset.end)
        self.assertEqual(series.status, "insufficient_history")
        self.assertEqual(series.points, [])

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_selection_uses_exact_cv_arguments(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        statsforecast_type.return_value.cross_validation.return_value = make_cv_result()

        champion = select_champion(dataset)

        self.assertEqual(statsforecast_type.call_count, 4)
        self.assertEqual(statsforecast_type.return_value.cross_validation.call_count, 4)
        for call in statsforecast_type.return_value.cross_validation.call_args_list:
            self.assertIs(call.kwargs["df"], dataset.frame)
            self.assertEqual(call.kwargs["h"], 96)
            self.assertEqual(call.kwargs["step_size"], 96)
            self.assertEqual(call.kwargs["n_windows"], 7)
        self.assertEqual(champion.model_name, "SeasonalNaive")

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_failed_candidate_is_skipped_without_affecting_other_cv_runs(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = [Mock() for _ in range(4)]
        engines[0].cross_validation.return_value = self._cv_for(
            "SeasonalNaive", [110.0, 110.0]
        )
        engines[1].cross_validation.side_effect = RuntimeError("ETS failed")
        engines[2].cross_validation.return_value = self._cv_for(
            "AutoARIMA", [101.0, 101.0]
        )
        engines[3].cross_validation.return_value = self._cv_for(
            "MSTL", [105.0, 105.0]
        )
        statsforecast_type.side_effect = engines

        champion = select_champion(dataset)

        self.assertEqual(champion.model_name, "AutoARIMA")
        self.assertAlmostEqual(champion.cv_mape_percent, 1.0)

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_candidate_setup_failure_is_also_isolated(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = []
        for model_name, values in (
            ("AutoETS", [101.0, 101.0]),
            ("AutoARIMA", [105.0, 105.0]),
            ("MSTL", [110.0, 110.0]),
        ):
            engine = Mock()
            engine.cross_validation.return_value = self._cv_for(model_name, values)
            engines.append(engine)
        statsforecast_type.side_effect = [RuntimeError("setup failed"), *engines]

        champion = select_champion(dataset)

        self.assertEqual(champion.model_name, "AutoETS")

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_selection_excludes_zero_and_imputed_actuals(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        ds = pd.date_range(dataset.start, periods=3, freq="15min")
        dataset = replace(
            dataset,
            imputed_keys=frozenset(
                {("station_total_load", ds[1].to_pydatetime())}
            ),
        )
        predictions = {
            "SeasonalNaive": [999.0, 200.0, 100.0],
            "AutoETS": [0.0, 101.0, 101.0],
            "AutoARIMA": [0.0, 105.0, 105.0],
            "MSTL": [0.0, 110.0, 110.0],
        }
        engines = []
        for model_name, values in predictions.items():
            engine = Mock()
            engine.cross_validation.return_value = pd.DataFrame(
                {
                    "unique_id": ["station_total_load"] * 3,
                    "ds": ds,
                    "cutoff": [ds[0] - timedelta(minutes=15)] * 3,
                    "y": [0.0, 100.0, 100.0],
                    model_name: values,
                }
            )
            engines.append(engine)
        statsforecast_type.side_effect = engines

        champion = select_champion(dataset)

        self.assertEqual(champion.model_name, "SeasonalNaive")
        self.assertEqual(champion.cv_mape_percent, 0.0)

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_candidate_with_partial_nan_predictions_is_rejected(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = []
        for model_name, values in (
            ("SeasonalNaive", [100.0, np.nan]),
            ("AutoETS", [101.0, 101.0]),
            ("AutoARIMA", [105.0, 105.0]),
            ("MSTL", [110.0, 110.0]),
        ):
            engine = Mock()
            engine.cross_validation.return_value = self._cv_for(model_name, values)
            engines.append(engine)
        statsforecast_type.side_effect = engines

        champion = select_champion(dataset)

        self.assertEqual(champion.model_name, "AutoETS")
        self.assertEqual(champion.cv_mape_percent, 1.0)

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_candidate_with_infinite_predictions_is_rejected(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = [Mock() for _ in range(4)]
        engines[0].cross_validation.return_value = self._cv_for(
            "SeasonalNaive", [100.0, np.inf]
        )
        for engine in engines[1:]:
            engine.cross_validation.side_effect = RuntimeError("candidate failed")
        statsforecast_type.side_effect = engines

        with self.assertRaises(M3Error) as raised:
            select_champion(dataset)

        self.assertEqual(raised.exception.code, "model_selection_failed")

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_all_nonfinite_candidate_outputs_fail_safely(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = []
        for model_name, values in (
            ("SeasonalNaive", [np.nan, np.nan]),
            ("AutoETS", [np.inf, np.inf]),
            ("AutoARIMA", [-np.inf, -np.inf]),
            ("MSTL", [np.nan, np.inf]),
        ):
            engine = Mock()
            engine.cross_validation.return_value = self._cv_for(model_name, values)
            engines.append(engine)
        statsforecast_type.side_effect = engines

        with self.assertRaises(M3Error) as raised:
            select_champion(dataset)

        self.assertEqual(raised.exception.code, "model_selection_failed")
        self.assertEqual(
            raised.exception.message, "all StatsForecast candidates failed"
        )

    @patch(
        "m3.worker.domain.forecasting.AutoETS",
        side_effect=RuntimeError("AutoETS constructor failed"),
    )
    def test_real_model_constructor_failure_does_not_block_remaining_candidates(
        self, autoets_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )

        champion = select_champion(dataset)

        self.assertIn(champion.model_name, {"SeasonalNaive", "AutoARIMA", "MSTL"})
        self.assertTrue(np.isfinite(champion.cv_mape_percent))
        autoets_type.assert_called_once_with(season_length=96, alias="AutoETS")

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_equal_scores_have_a_deterministic_winner(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        engines = []
        for model_name in ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"):
            engine = Mock()
            engine.cross_validation.return_value = self._cv_for(
                model_name, [101.0, 101.0]
            )
            engines.append(engine)
        statsforecast_type.side_effect = engines

        champion = select_champion(dataset)

        self.assertEqual(champion.model_name, "AutoARIMA")

    @patch("m3.worker.domain.forecasting.schedule_cross_validation", new=_scoring_cv_fixture)
    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_all_candidate_failures_raise_model_selection_error(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        statsforecast_type.return_value.cross_validation.side_effect = ValueError(
            "private detail"
        )

        with self.assertRaises(M3Error) as raised:
            select_champion(dataset)

        self.assertEqual(raised.exception.code, "model_selection_failed")
        self.assertNotIn("private detail", raised.exception.message)

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_seven_through_twenty_seven_days_use_warming_up_baseline(
        self, statsforecast_type
    ):
        for days in (7, 27):
            with self.subTest(days=days):
                dataset = build_training_dataset(
                    make_quarter_hour_points(days), "station_total_load"
                )
                champion = select_champion(dataset)
                self.assertEqual(champion.model_name, "SeasonalNaive")
                self.assertIsNone(champion.cv_mape_percent)
        statsforecast_type.assert_not_called()

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_forecast_emits_aligned_points_and_preserves_clipped_raw_value(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        as_of = dataset.end + timedelta(minutes=15)
        values = [-3.0] + [800.0] * 95
        offsets = LoadScheduleProfile.fit(dataset.frame, origin=as_of).offsets(
            pd.date_range(as_of, periods=96, freq="15min")
        )
        residuals = (np.asarray(values) - offsets).tolist()
        statsforecast_type.return_value.forecast.return_value = make_forecast_result(
            "AutoARIMA", as_of, residuals
        )
        champion = self._champion(dataset, "AutoARIMA")

        series = forecast_one(dataset, champion, as_of)

        self.assertEqual(series.status, "ok")
        self.assertEqual(series.model_name, "AutoARIMA")
        self.assertEqual(len(series.points), 96)
        self.assertEqual(series.points[0].data_time, as_of)
        self.assertEqual(
            series.points[0].target_time, as_of + timedelta(minutes=15)
        )
        self.assertEqual(series.points[-1].horizon_step, 96)
        self.assertEqual(series.points[0].raw_forecast, -3.0)
        self.assertEqual(series.points[0].forecast_value, 0.0)
        self.assertTrue(series.points[0].is_clipped)
        self.assertFalse(series.points[1].is_clipped)

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_off_grid_as_of_uses_floored_bucket_start_for_baseline_window(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        as_of = datetime.fromisoformat("2026-08-25T01:02:00+08:00")
        first_data_time = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
        statsforecast_type.return_value.forecast.return_value = make_forecast_result(
            "SeasonalNaive", first_data_time
        )

        series = forecast_one(
            dataset, self._champion(dataset, "SeasonalNaive"), as_of
        )

        self.assertEqual(
            series.points[0].data_time.isoformat(), "2026-08-25T01:00:00+08:00"
        )
        self.assertEqual(
            series.points[0].target_time.isoformat(), "2026-08-25T01:15:00+08:00"
        )
        self.assertEqual(
            series.points[-1].data_time.isoformat(), "2026-08-26T00:45:00+08:00"
        )
        self.assertEqual(
            series.points[-1].target_time.isoformat(), "2026-08-26T01:00:00+08:00"
        )

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_champion_forecast_failure_retries_only_seasonal_naive_safely(
        self, statsforecast_type
    ):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        as_of = dataset.end + timedelta(minutes=15)
        champion_engine = Mock()
        champion_engine.forecast.side_effect = RuntimeError("database password")
        fallback_engine = Mock()
        fallback_engine.forecast.return_value = make_forecast_result(
            "SeasonalNaive", as_of
        )
        statsforecast_type.side_effect = [champion_engine, fallback_engine]

        series = forecast_one(dataset, self._champion(dataset, "MSTL"), as_of)

        self.assertEqual(series.model_name, "SeasonalNaive")
        self.assertEqual(series.status, "degraded")
        self.assertEqual(series.fallback_reason, "RuntimeError")
        self.assertNotIn("database password", series.fallback_reason)
        self.assertEqual(statsforecast_type.call_count, 2)

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_seasonal_naive_forecast_failure_propagates(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(7), "station_total_load"
        )
        statsforecast_type.return_value.forecast.side_effect = KeyError("missing")

        with self.assertRaises(KeyError):
            forecast_one(
                dataset,
                seasonal_naive_champion(dataset),
                dataset.end + timedelta(minutes=15),
            )

        self.assertEqual(statsforecast_type.call_count, 1)

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_forecast_one_safe_returns_a_sanitized_error_after_fallback_failure(
        self, statsforecast_type
    ):
        """A failed SeasonalNaive fallback must not escape the per-series boundary."""
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        champion_engine = Mock()
        champion_engine.forecast.side_effect = RuntimeError("champion password")
        fallback_engine = Mock()
        fallback_engine.forecast.side_effect = RuntimeError("fallback password")
        statsforecast_type.side_effect = [champion_engine, fallback_engine]

        series = forecast_one_safe(
            dataset,
            self._champion(dataset, "MSTL"),
            dataset.end + timedelta(minutes=15),
        )

        self.assertEqual(series.unique_id, "station_total_load")
        self.assertEqual(series.status, "error")
        self.assertEqual(series.points, [])
        self.assertEqual(series.fallback_reason, "forecast_failed")
        self.assertNotIn("champion password", series.fallback_reason)
        self.assertNotIn("fallback password", series.fallback_reason)
        self.assertEqual(statsforecast_type.call_count, 2)
        for engine in (champion_engine, fallback_engine):
            engine.forecast.assert_called_once()
            self.assertEqual(engine.forecast.call_args.kwargs["h"], 96)
            residual_frame = engine.forecast.call_args.kwargs["df"]
            self.assertEqual(list(residual_frame["ds"]), list(dataset.frame["ds"]))
            self.assertTrue(np.allclose(residual_frame["y"], 0.0))

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_warming_up_forecast_is_truthfully_flagged(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(7), "station_total_load"
        )
        as_of = dataset.end + timedelta(minutes=15)
        statsforecast_type.return_value.forecast.return_value = make_forecast_result(
            "SeasonalNaive", as_of
        )

        series = forecast_one(
            dataset, seasonal_naive_champion(dataset), as_of
        )

        self.assertEqual(series.status, "warming_up")
        self.assertIsNone(series.fallback_reason)

    @patch("m3.worker.domain.forecasting.StatsForecast")
    def test_misaligned_forecast_is_rejected(self, statsforecast_type):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )
        as_of = dataset.end + timedelta(minutes=15)
        statsforecast_type.return_value.forecast.return_value = make_forecast_result(
            "AutoETS", as_of + timedelta(minutes=15)
        )

        with self.assertRaises(M3Error) as raised:
            forecast_one(dataset, self._champion(dataset, "AutoETS"), as_of)

        self.assertEqual(raised.exception.code, "forecast_alignment_invalid")

    @staticmethod
    def _cv_for(model_name, predictions):
        ds = pd.date_range("2026-08-18T00:00:00+08:00", periods=2, freq="15min")
        return pd.DataFrame(
            {
                "unique_id": ["station_total_load"] * 2,
                "ds": ds,
                "cutoff": [ds[0] - timedelta(minutes=15)] * 2,
                "y": [100.0, 100.0],
                model_name: predictions,
            }
        )

    @staticmethod
    def _champion(dataset, model_name):
        return Champion(
            model_name=model_name,
            cv_mape_percent=1.0,
            selected_at=pd.Timestamp("2026-08-25T12:00:00+08:00"),
            training_start=pd.Timestamp(dataset.start),
            training_end=pd.Timestamp(dataset.end),
            statsforecast_version="2.1.1",
        )


class M3ForecastingSmokeTests(unittest.TestCase):
    def test_all_four_models_forecast_fixed_fixture(self):
        dataset = build_training_dataset(
            make_quarter_hour_points(28), "station_total_load"
        )

        result = RealStatsForecast(
            models=candidate_models(), freq="15min", n_jobs=1
        ).forecast(df=dataset.frame, h=96)

        self.assertEqual(len(result), 96)
        for column in ("SeasonalNaive", "AutoETS", "AutoARIMA", "MSTL"):
            self.assertTrue(result[column].notna().all(), column)
            self.assertTrue(np.isfinite(result[column]).all(), column)


if __name__ == "__main__":
    unittest.main()
