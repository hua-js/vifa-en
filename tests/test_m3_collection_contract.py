import json
from datetime import datetime
import os
from pathlib import Path
import re
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from m3_worker.contracts import AcceptanceContext, LatestSnapshot, ObservationPoint
from m3_worker.domain.evaluation import MetricResult
from m3_worker.errors import M3Error
from m3_worker.services.acceptance_service import (
    ACTUAL_UPDATE_FIELDS,
    BACKFILL_FIELDS,
    EVALUATION_FIELDS,
    METRIC_FIELDS,
    WRITING_BATCH_FIELDS,
    AcceptanceService,
)
from m3_worker.sinks.forecast_sink import (
    POINT_HASH_FIELDS,
    ForecastSink,
    canonical_hash,
)
from tests.test_m3_nocobase_sink import FakeNocoBase, make_acceptance_records


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "m3/contracts/nocobase_collections.json"
SMOKE_PATH = ROOT / "m3/contracts/latest-smoke-record.json"
GUIDE_PATH = ROOT / "m3/部署说明.md"
FLOW_PATH = ROOT / "m3/node_red/energy_forecast_flow.json"
NODE_CONTRACT_PATH = ROOT / "m3/node_red/forecast_contract.js"

COLLECTION_FIELDS = {
    "energy_forecast_latest": {
        "id": ("bigint", False),
        "station_id": ("text", False),
        "as_of": ("timestamptz", False),
        "generated_at": ("timestamptz", False),
        "source_data_end": ("timestamptz", False),
        "status": ("text", False),
        "series_payload": ("jsonb", False),
        "model_manifest": ("jsonb", False),
        "content_hash": ("text", False),
        "updated_at": ("timestamptz", False),
    },
    "energy_forecast_batches": {
        "id": ("bigint", False),
        "station_id": ("text", False),
        "acceptance_run_id": ("text", False),
        "issued_at": ("timestamptz", False),
        "forecast_start_time": ("timestamptz", False),
        "forecast_end_time": ("timestamptz", False),
        "status": ("text", False),
        "write_state": ("text", False),
        "model_manifest": ("jsonb", False),
        "point_templates": ("jsonb", False),
        "content_hash": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "energy_forecast_points": {
        "id": ("bigint", False),
        "batch_id": ("bigint", False),
        "unique_id": ("text", False),
        "data_time": ("timestamptz", False),
        "target_time": ("timestamptz", False),
        "horizon_step": ("smallint", False),
        "model_name": ("text", False),
        "raw_forecast": ("numeric(14,6)", False),
        "forecast_value": ("numeric(14,6)", False),
        "is_clipped": ("boolean", False),
        "actual_value": ("numeric(14,6)", True),
        "actual_quality": ("text", True),
        "actual_source_revision": ("bigint", True),
        "actual_recorded_at": ("timestamptz", True),
        "evaluated_at": ("timestamptz", True),
        "absolute_percentage_error": ("numeric(14,8)", True),
    },
    "energy_forecast_evaluations": {
        "id": ("bigint", False),
        "station_id": ("text", False),
        "acceptance_run_id": ("text", False),
        "evaluation_key": ("text", False),
        "window_start": ("timestamptz", False),
        "window_end": ("timestamptz", False),
        "expected_count": ("smallint", False),
        "valid_count": ("smallint", False),
        "zero_actual_count": ("smallint", False),
        "mape_percent": ("numeric(10,6)", True),
        "mae": ("numeric(14,6)", True),
        "smape_percent": ("numeric(10,6)", True),
        "wape_percent": ("numeric(10,6)", True),
        "median_ape_percent": ("numeric(10,6)", True),
        "p90_ape_percent": ("numeric(10,6)", True),
        "outcome": ("text", False),
        "calculated_at": ("timestamptz", False),
    },
}

DENIED_ACTIONS = ["destroy", "delete", "export", "import"]
EXPECTED_POINT_COUNT = 2 * 96

DASHBOARD_LIST_MATRIX = {
    "energy_forecast_latest": {
        "read": [
            "station_id",
            "as_of",
            "generated_at",
            "source_data_end",
            "status",
            "series_payload",
            "model_manifest",
            "content_hash",
        ],
        "filter": ["station_id"],
        "sort": [],
        "write": [],
        "record_key": [],
    },
    "energy_forecast_batches": {
        "read": [
            "station_id",
            "acceptance_run_id",
            "issued_at",
            "forecast_start_time",
            "write_state",
        ],
        "filter": ["station_id", "write_state"],
        "sort": ["issued_at"],
        "write": [],
        "record_key": [],
    },
    "energy_forecast_evaluations": {
        "read": [
            "station_id",
            "acceptance_run_id",
            "evaluation_key",
            "expected_count",
            "valid_count",
            "zero_actual_count",
            "mape_percent",
            "mae",
            "smape_percent",
            "wape_percent",
            "median_ape_percent",
            "p90_ape_percent",
            "outcome",
        ],
        "filter": ["station_id", "acceptance_run_id"],
        "sort": [],
        "write": [],
        "record_key": [],
    },
}


def _load_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _collections(contract):
    return {item["name"]: item for item in contract["collections"]}


def _guide_python_script(environment_variable):
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"uv run python -c '([^'\n]+)'", guide)
    return next(script for script in scripts if environment_variable in script)


class CollectionContractTests(unittest.TestCase):
    def test_four_collections_have_exact_fields_and_database_types(self):
        contract = _load_contract()
        collections = _collections(contract)

        self.assertEqual(contract["version"], 1)
        self.assertEqual(set(collections), set(COLLECTION_FIELDS))
        self.assertEqual(contract["database_policy"]["identifier_style"], "lowercase_snake_case")
        self.assertEqual(contract["database_policy"]["timestamp_type"], "timestamptz")
        self.assertEqual(contract["database_policy"]["worker_access"], "http_only")
        self.assertEqual(contract["database_policy"]["jsonb_indexes"], [])

        for name, expected in COLLECTION_FIELDS.items():
            fields = collections[name]["fields"]
            self.assertEqual(set(fields), set(expected), name)
            self.assertEqual(
                {field: (definition["type"], definition["nullable"]) for field, definition in fields.items()},
                expected,
                name,
            )
            self.assertEqual(
                fields["id"],
                {
                    "type": "bigint",
                    "nullable": False,
                    "identity": True,
                    "primary_key": True,
                },
                name,
            )
            for field, definition in fields.items():
                self.assertRegex(field, r"^[a-z][a-z0-9_]*$", (name, field))
                if "time" in field or field.endswith("_at") or field == "as_of":
                    self.assertEqual(definition["type"], "timestamptz", (name, field))

        self.assertEqual(
            collections["energy_forecast_batches"]["fields"]["created_at"]["generated_on_create"],
            "current_timestamp",
        )
        self.assertEqual(
            collections["energy_forecast_latest"]["fields"]["updated_at"],
            {
                "type": "timestamptz",
                "nullable": False,
                "client_writable": False,
                "server_managed": {
                    "on_create": "current_timestamp",
                    "on_update": "current_timestamp",
                },
            },
        )

    def test_business_keys_foreign_key_and_leftmost_index_coverage_are_exact(self):
        collections = _collections(_load_contract())
        expected_unique = {
            "energy_forecast_latest": ["station_id"],
            "energy_forecast_batches": ["station_id", "acceptance_run_id", "issued_at"],
            "energy_forecast_points": ["batch_id", "unique_id", "data_time"],
            "energy_forecast_evaluations": ["station_id", "acceptance_run_id", "evaluation_key"],
        }
        for name, fields in expected_unique.items():
            self.assertEqual(collections[name]["unique_constraints"][0]["fields"], fields, name)
            unique_index = next(index for index in collections[name]["indexes"] if index["unique"])
            self.assertEqual(unique_index["fields"], fields, name)
            self.assertEqual(unique_index["provided_by"], "unique_constraint", name)

        batches_index = collections["energy_forecast_batches"]["indexes"][0]
        self.assertEqual(batches_index["access_pattern"]["equality_prefix"], ["station_id", "acceptance_run_id"])
        self.assertEqual(batches_index["access_pattern"]["range_suffix"], ["issued_at"])

        points = collections["energy_forecast_points"]
        self.assertEqual(
            points["foreign_keys"],
            [
                {
                    "name": "energy_forecast_points_batch_id_fkey",
                    "fields": ["batch_id"],
                    "references": {"collection": "energy_forecast_batches", "fields": ["id"]},
                    "on_delete": "restrict",
                    "index_coverage": "energy_forecast_points_batch_unique_data_time_key",
                }
            ],
        )
        covering = next(index for index in points["indexes"] if index["name"] == points["foreign_keys"][0]["index_coverage"])
        self.assertEqual(covering["fields"][0], "batch_id")

        evaluations_index = next(index for index in collections["energy_forecast_evaluations"]["indexes"] if not index["unique"])
        self.assertEqual(evaluations_index["fields"], ["station_id", "acceptance_run_id"])
        self.assertEqual(evaluations_index["access_pattern"]["equality_prefix"], ["station_id", "acceptance_run_id"])
        self.assertEqual(evaluations_index["access_pattern"]["range_suffix"], [])

    def test_all_enumeration_range_temporal_and_physical_checks_are_named(self):
        collections = _collections(_load_contract())
        expected_checks = {
            "energy_forecast_latest": {"energy_forecast_latest_status_check"},
            "energy_forecast_batches": {
                "energy_forecast_batches_window_check",
                "energy_forecast_batches_status_check",
                "energy_forecast_batches_write_state_check",
            },
            "energy_forecast_points": {
                "energy_forecast_points_target_interval_check",
                "energy_forecast_points_horizon_step_check",
                "energy_forecast_points_unique_id_check",
                "energy_forecast_points_load_forecast_check",
                "energy_forecast_points_soc_forecast_check",
                "energy_forecast_points_actual_quality_check",
            },
            "energy_forecast_evaluations": {
                "energy_forecast_evaluations_key_check",
                "energy_forecast_evaluations_window_check",
                "energy_forecast_evaluations_series_counts_check",
                "energy_forecast_evaluations_overall_counts_check",
                "energy_forecast_evaluations_count_partition_check",
                "energy_forecast_evaluations_mape_check",
                "energy_forecast_evaluations_overall_metrics_check",
                "energy_forecast_evaluations_outcome_check",
            },
        }
        for name, expected_names in expected_checks.items():
            checks = {item["name"]: item for item in collections[name]["check_constraints"]}
            self.assertEqual(set(checks), expected_names, name)

        latest_status = collections["energy_forecast_latest"]["check_constraints"][0]
        self.assertEqual(latest_status["allowed_values"], ["ok", "warming_up", "degraded"])

        point_checks = {item["name"]: item for item in collections["energy_forecast_points"]["check_constraints"]}
        self.assertEqual(point_checks["energy_forecast_points_horizon_step_check"]["range"], {"minimum": 1, "maximum": 96})
        self.assertEqual(point_checks["energy_forecast_points_target_interval_check"]["interval_minutes"], 15)
        self.assertEqual(point_checks["energy_forecast_points_actual_quality_check"]["allowed_values"], ["valid", "invalid"])
        self.assertTrue(point_checks["energy_forecast_points_actual_quality_check"]["null_allowed"])
        self.assertEqual(point_checks["energy_forecast_points_load_forecast_check"]["minimum"], 0)
        self.assertEqual(point_checks["energy_forecast_points_soc_forecast_check"]["range"], {"minimum": 0, "maximum": 100})

        evaluation_checks = {item["name"]: item for item in collections["energy_forecast_evaluations"]["check_constraints"]}
        self.assertEqual(EXPECTED_POINT_COUNT, 192)
        self.assertEqual(
            evaluation_checks["energy_forecast_evaluations_key_check"]["allowed_values"],
            ["station_total_load", "storage_soc", "overall"],
        )
        self.assertEqual(evaluation_checks["energy_forecast_evaluations_window_check"]["interval_days"], 7)
        self.assertEqual(evaluation_checks["energy_forecast_evaluations_series_counts_check"]["expected_count"], 672)
        self.assertEqual(evaluation_checks["energy_forecast_evaluations_overall_counts_check"]["expected_count"], 1344)
        self.assertEqual(evaluation_checks["energy_forecast_evaluations_mape_check"]["minimum"], 0)
        self.assertEqual(
            evaluation_checks["energy_forecast_evaluations_overall_metrics_check"]["fields"],
            [
                "mape_percent",
                "mae",
                "smape_percent",
                "wape_percent",
                "median_ape_percent",
                "p90_ape_percent",
            ],
        )
        self.assertEqual(
            evaluation_checks["energy_forecast_evaluations_outcome_check"]["allowed_values"],
            ["in_progress", "passed", "failed", "insufficient_data"],
        )

    def test_application_invariants_cover_atomic_latest_and_immutable_acceptance(self):
        collections = _collections(_load_contract())
        self.assertEqual(
            set(collections["energy_forecast_latest"]["application_invariants"]),
            {
                "exactly_two_configured_series_reach_terminal_state_before_publish",
                "predictable_series_have_exactly_96_points",
                "unpredictable_series_have_empty_points_and_explicit_reason",
                "same_as_of_and_content_hash_is_idempotent",
                "same_as_of_with_different_content_hash_is_conflict",
                "publish_with_one_update_or_create_after_complete_validation",
            },
        )
        self.assertIn("point_templates_remain_immutable_and_contain_exactly_192_prediction_points", collections["energy_forecast_batches"]["application_invariants"])
        self.assertIn("readers_ignore_write_state_writing", collections["energy_forecast_batches"]["application_invariants"])
        self.assertIn("replay_compares_immutable_forecast_fields_and_never_updates_them", collections["energy_forecast_points"]["application_invariants"])
        self.assertIn("batch_completes_only_after_192_points_and_content_hash_verify", collections["energy_forecast_points"]["application_invariants"])

    def test_worker_permissions_are_per_collection_action_and_field(self):
        contract = _load_contract()
        worker = contract["worker_role"]
        permissions = worker["collections"]
        self.assertEqual(set(permissions), set(COLLECTION_FIELDS))
        self.assertEqual(worker["database_access"], "none")
        self.assertEqual(worker["denied_actions"], DENIED_ACTIONS)
        self.assertEqual(
            worker["allowed_actions"],
            ["list", "update", "updateOrCreate", "firstOrCreate"],
        )

        expected_actions = {
            "energy_forecast_latest": ["list", "updateOrCreate"],
            "energy_forecast_batches": ["list", "update", "firstOrCreate"],
            "energy_forecast_points": ["list", "update", "firstOrCreate"],
            "energy_forecast_evaluations": ["updateOrCreate"],
        }
        for name, allowed in expected_actions.items():
            item = permissions[name]
            self.assertEqual(item["allowed_actions"], allowed, name)
            self.assertEqual(item["denied_actions"], DENIED_ACTIONS, name)
            self.assertEqual(set(item["fields_by_action"]), set(allowed), name)

        batch = permissions["energy_forecast_batches"]
        self.assertEqual(batch["fields_by_action"]["update"]["write"], ["write_state"])
        self.assertEqual(
            set(batch["protected_fields"]),
            set(COLLECTION_FIELDS["energy_forecast_batches"]) - {"id", "write_state", "created_at"},
        )

        point = permissions["energy_forecast_points"]
        actual_fields = {
            "actual_value",
            "actual_quality",
            "actual_source_revision",
            "actual_recorded_at",
            "evaluated_at",
            "absolute_percentage_error",
        }
        immutable_point_fields = {
            "batch_id",
            "unique_id",
            "data_time",
            "target_time",
            "horizon_step",
            "model_name",
            "raw_forecast",
            "forecast_value",
            "is_clipped",
        }
        self.assertEqual(set(point["fields_by_action"]["update"]["write"]), actual_fields)
        self.assertEqual(set(point["protected_fields"]), immutable_point_fields)
        self.assertEqual(set(point["fields_by_action"]["firstOrCreate"]["write"]), immutable_point_fields)

        evaluation = permissions["energy_forecast_evaluations"]
        evaluation_upsert = set(COLLECTION_FIELDS["energy_forecast_evaluations"]) - {"id"}
        self.assertEqual(set(evaluation["upsert_fields"]), evaluation_upsert)
        self.assertEqual(set(evaluation["fields_by_action"]["updateOrCreate"]["write"]), evaluation_upsert)

        prerequisite_gate = worker["installed_api_documentation"]
        self.assertEqual(prerequisite_gate["gate"], "hard_pre_production")
        self.assertEqual(
            prerequisite_gate["evidence_required"],
            [
                "action_names_methods_body_params_and_response_envelopes",
                "separate_read_filter_sort_write_record_key_permissions",
                "exact_list_field_filter_and_sort_permissions",
                "points_batch_dotted_relation_filter_permissions",
            ],
        )
        self.assertEqual(
            prerequisite_gate["default_not_granted"],
            {
                "updateOrCreate": ["get", "create", "update"],
                "firstOrCreate": ["get", "create"],
            },
        )
        self.assertEqual(prerequisite_gate["verified_additional_grants"], {})
        self.assertEqual(
            prerequisite_gate["if_additional_grant_is_required"],
            "stop_update_machine_contract_and_tests_then_rereview",
        )

    def test_points_batch_relationship_matches_task9_dotted_filters(self):
        contract = _load_contract()
        self.assertEqual(
            contract["relationships"],
            [
                {
                    "name": "energy_forecast_points_batch_belongs_to",
                    "accessor": "batch",
                    "kind": "belongs_to",
                    "source_collection": "energy_forecast_points",
                    "source_foreign_key": "batch_id",
                    "target_collection": "energy_forecast_batches",
                    "target_key": "id",
                    "client_writable": False,
                    "relationship_object_persisted": False,
                    "required_dotted_filters": [
                        "batch.station_id",
                        "batch.acceptance_run_id",
                        "batch.write_state",
                    ],
                    "installed_api_documentation": {
                        "gate": "hard_pre_production",
                        "evidence_required": [
                            "association_alias_batch_uses_existing_batch_id_foreign_key",
                            "dotted_filter_syntax_and_permissions",
                            "read_only_synthetic_relation_filter_probe",
                        ],
                        "if_unsupported": "stop_update_machine_contract_and_tests_then_rereview",
                        "alternate_query_path": "forbidden",
                    },
                }
            ],
        )

        class CapturingApi:
            def __init__(self):
                self.calls = []

            def list_records(self, collection, *, filter, fields, sort=None):
                self.calls.append(
                    {
                        "collection": collection,
                        "filter": filter,
                        "fields": fields,
                        "sort": sort,
                    }
                )
                return []

        api = CapturingApi()
        service = AcceptanceService(
            context_source=None,
            observation_source=None,
            api=api,
            sink=None,
            forecast_service=None,
            now=lambda: None,
        )
        start = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
        context = AcceptanceContext(
            active=True,
            acceptance_run_id="run-20260825",
            window_start=start,
            window_end=datetime.fromisoformat("2026-09-01T01:00:00+08:00"),
        )
        self.assertEqual(
            service._list_backfill_points(
                "station-1",
                context,
                datetime.fromisoformat("2026-08-25T02:00:00+08:00"),
            ),
            [],
        )
        relationship = contract["relationships"][0]
        for call in api.calls:
            self.assertEqual(call["collection"], relationship["source_collection"])
            dotted = [field for field in call["filter"] if "." in field]
            self.assertEqual(dotted, relationship["required_dotted_filters"])
            self.assertEqual(call["fields"], list(BACKFILL_FIELDS))
            self.assertEqual(call["sort"], ["data_time"])

        with self.assertRaises(M3Error):
            service._series_evaluation(
                "station-1", "run-20260825", "station_total_load", context
            )
        evaluation_call = api.calls[-1]
        self.assertEqual(
            [field for field in evaluation_call["filter"] if "." in field],
            relationship["required_dotted_filters"],
        )
        self.assertEqual(evaluation_call["fields"], list(EVALUATION_FIELDS))
        self.assertEqual(evaluation_call["sort"], ["data_time"])

        points = _collections(contract)["energy_forecast_points"]
        self.assertEqual(points["fields"]["batch_id"]["type"], "bigint")
        self.assertEqual(points["foreign_keys"][0]["on_delete"], "restrict")
        self.assertEqual(points["indexes"][0]["fields"][0], "batch_id")
        self.assertNotIn("batch", points["fields"])

    def test_worker_field_channels_are_exact_and_match_real_request_construction(self):
        worker = _load_contract()["worker_role"]
        for channel in (
            "unlisted_read_fields",
            "unlisted_filter_fields",
            "unlisted_sort_fields",
            "unlisted_write_fields",
            "unlisted_record_key_fields",
        ):
            self.assertEqual(worker[channel], "deny")

        permissions = worker["collections"]
        expected_lists = {
            "energy_forecast_latest": {
                "read": ["id", "station_id", "as_of", "content_hash"],
                "filter": ["station_id"],
                "sort": [],
                "write": [],
                "record_key": [],
            },
            "energy_forecast_batches": {
                "read": list(WRITING_BATCH_FIELDS),
                "filter": ["write_state"],
                "sort": ["issued_at"],
                "write": [],
                "record_key": [],
            },
            "energy_forecast_points": {
                "read": [
                    "id",
                    "unique_id",
                    "data_time",
                    "target_time",
                    "horizon_step",
                    "model_name",
                    "raw_forecast",
                    "forecast_value",
                    "is_clipped",
                    "actual_value",
                    "actual_quality",
                    "actual_source_revision",
                ],
                "filter": [
                    "batch_id",
                    "batch.station_id",
                    "batch.acceptance_run_id",
                    "batch.write_state",
                    "unique_id",
                    "data_time",
                ],
                "sort": ["unique_id", "data_time"],
                "write": [],
                "record_key": [],
            },
        }
        for collection, fields in expected_lists.items():
            self.assertEqual(
                permissions[collection]["fields_by_action"]["list"], fields
            )

        expected_write_channels = {
            ("energy_forecast_latest", "updateOrCreate"): {
                "filter": ["station_id"],
                "sort": [],
                "record_key": [],
            },
            ("energy_forecast_batches", "firstOrCreate"): {
                "filter": ["station_id", "acceptance_run_id", "issued_at"],
                "sort": [],
                "record_key": [],
            },
            ("energy_forecast_batches", "update"): {
                "filter": [],
                "sort": [],
                "record_key": ["id"],
            },
            ("energy_forecast_points", "firstOrCreate"): {
                "filter": ["batch_id", "unique_id", "data_time"],
                "sort": [],
                "record_key": [],
            },
            ("energy_forecast_points", "update"): {
                "filter": [],
                "sort": [],
                "record_key": ["id"],
            },
            ("energy_forecast_evaluations", "updateOrCreate"): {
                "filter": ["station_id", "acceptance_run_id", "evaluation_key"],
                "sort": [],
                "record_key": [],
            },
        }
        for (collection, action), expected in expected_write_channels.items():
            declared = permissions[collection]["fields_by_action"][action]
            for channel, fields in expected.items():
                self.assertEqual(declared[channel], fields, (collection, action, channel))
            self.assertEqual(
                set(declared), {"read", "filter", "sort", "write", "record_key"}
            )

        smoke = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))["values"]
        domain = dict(smoke)
        domain["series"] = domain.pop("series_payload")
        latest_api = FakeNocoBase()
        ForecastSink(latest_api).publish_latest(LatestSnapshot.model_validate(domain))
        latest_calls = dict(latest_api.actions)
        self.assertEqual(
            set(latest_calls["energy_forecast_latest:list"]["filter"]),
            set(permissions["energy_forecast_latest"]["fields_by_action"]["list"]["filter"]),
        )
        self.assertEqual(
            set(latest_calls["energy_forecast_latest:updateOrCreate"]["filter"]),
            set(permissions["energy_forecast_latest"]["fields_by_action"]["updateOrCreate"]["filter"]),
        )

        acceptance_api = FakeNocoBase()
        batch, points = make_acceptance_records()
        ForecastSink(acceptance_api).publish_acceptance(batch, points)
        first_calls = {}
        for action, arguments in acceptance_api.actions:
            first_calls.setdefault(action, arguments)
        self.assertEqual(
            list(first_calls["energy_forecast_batches:firstOrCreate"]["filter"]),
            permissions["energy_forecast_batches"]["fields_by_action"]["firstOrCreate"]["filter"],
        )
        self.assertEqual(
            list(first_calls["energy_forecast_points:firstOrCreate"]["filter"]),
            permissions["energy_forecast_points"]["fields_by_action"]["firstOrCreate"]["filter"],
        )
        self.assertEqual(
            list(first_calls["energy_forecast_batches:update"]),
            ["record_id", "values"],
        )
        self.assertEqual(
            permissions["energy_forecast_batches"]["fields_by_action"]["update"]["record_key"],
            ["id"],
        )

        point_read_union = set(POINT_HASH_FIELDS) | set(BACKFILL_FIELDS) | set(EVALUATION_FIELDS)
        self.assertEqual(
            set(permissions["energy_forecast_points"]["fields_by_action"]["list"]["read"]),
            point_read_union,
        )
        self.assertEqual(
            set(first_calls["energy_forecast_points:list"]["filter"]),
            {"batch_id"},
        )
        self.assertEqual(
            first_calls["energy_forecast_points:list"]["sort"],
            ["unique_id", "data_time"],
        )
        self.assertEqual(
            set(first_calls["energy_forecast_points:list"]["fields"]),
            set(POINT_HASH_FIELDS),
        )

        class ListApi:
            def __init__(self):
                self.calls = []

            def list_records(self, collection, *, filter, fields, sort=None):
                self.calls.append((collection, filter, fields, sort))
                return []

        list_api = ListApi()
        self.assertEqual(
            AcceptanceService(
                context_source=None,
                observation_source=None,
                api=list_api,
                sink=None,
                forecast_service=None,
                now=lambda: None,
            ).reconcile_writing_batches(),
            0,
        )
        collection, filter_fields, read_fields, sort_fields = list_api.calls[0]
        self.assertEqual(collection, "energy_forecast_batches")
        self.assertEqual(
            list(filter_fields),
            permissions[collection]["fields_by_action"]["list"]["filter"],
        )
        self.assertEqual(read_fields, list(WRITING_BATCH_FIELDS))
        self.assertEqual(
            sort_fields,
            permissions[collection]["fields_by_action"]["list"]["sort"],
        )

        class EvaluationApi:
            def __init__(self):
                self.calls = []

            def update_or_create(self, collection, filter, values):
                self.calls.append((collection, filter, values))
                return {"id": len(self.calls), **values}

        class SyntheticEvaluationService(AcceptanceService):
            def _series_evaluation(
                self, station_id, acceptance_run_id, unique_id, context
            ):
                metric = MetricResult(
                    672, 672, 0, 1.0, 1.0, 1.0, "passed", 1.0, 1.0, 1.0
                )
                return metric, {
                    "station_id": station_id,
                    "acceptance_run_id": acceptance_run_id,
                    "evaluation_key": unique_id,
                    "window_start": context.window_start.isoformat(),
                    "window_end": context.window_end.isoformat(),
                    "expected_count": 672,
                    "valid_count": 672,
                    "zero_actual_count": 0,
                    "mape_percent": 1.0,
                    "mae": 1.0,
                    "smape_percent": 1.0,
                    "wape_percent": 1.0,
                    "median_ape_percent": 1.0,
                    "p90_ape_percent": 1.0,
                    "outcome": "passed",
                }

        evaluation_api = EvaluationApi()
        start = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
        context = AcceptanceContext(
            active=True,
            acceptance_run_id="run-20260825",
            window_start=start,
            window_end=datetime.fromisoformat("2026-09-01T01:00:00+08:00"),
        )
        SyntheticEvaluationService(
            context_source=None,
            observation_source=None,
            api=evaluation_api,
            sink=None,
            forecast_service=None,
            now=lambda: datetime.fromisoformat("2026-09-01T01:02:00+08:00"),
        )._recalculate_locked("station-1", "run-20260825", context)
        expected_evaluation_filter = permissions["energy_forecast_evaluations"][
            "fields_by_action"
        ]["updateOrCreate"]["filter"]
        for collection, filter_fields, _ in evaluation_api.calls:
            self.assertEqual(collection, "energy_forecast_evaluations")
            self.assertEqual(list(filter_fields), expected_evaluation_filter)

        class ActualApi:
            def __init__(self):
                self.calls = []

            def update_record(self, collection, record_id, values):
                self.calls.append((collection, record_id, values))
                return {
                    "id": record_id,
                    "unique_id": "station_total_load",
                    "data_time": "2026-08-25T01:00:00+08:00",
                    "forecast_value": 100.0,
                    **values,
                }

        class SyntheticBackfillService(AcceptanceService):
            def _list_backfill_points(self, station_id, context, completed_end):
                parsed = datetime.fromisoformat("2026-08-25T01:00:00+08:00")
                return [
                    {
                        "id": 77,
                        "unique_id": "station_total_load",
                        "data_time": parsed.isoformat(),
                        "data_time_parsed": parsed,
                        "forecast_value": 100.0,
                        "forecast_value_parsed": 100.0,
                        "actual_source_revision": None,
                    }
                ]

            def _pull_actuals(self, station_id, start, end):
                actual = ObservationPoint(
                    unique_id="station_total_load",
                    ds=datetime.fromisoformat("2026-08-25T01:00:00+08:00"),
                    y=90.0,
                    quality="valid",
                    source_revision=1,
                )
                return {(actual.unique_id, actual.ds): actual}

        actual_api = ActualApi()
        updated = SyntheticBackfillService(
            context_source=None,
            observation_source=None,
            api=actual_api,
            sink=None,
            forecast_service=None,
            now=lambda: datetime.fromisoformat("2026-08-25T01:20:05+08:00"),
        )._backfill_locked(
            "station-1",
            datetime.fromisoformat("2026-08-25T01:20:00+08:00"),
            context,
        )
        self.assertEqual(updated, 1)
        collection, record_id, values = actual_api.calls[0]
        self.assertEqual(collection, "energy_forecast_points")
        self.assertEqual(record_id, 77)
        self.assertEqual(set(values), ACTUAL_UPDATE_FIELDS)
        self.assertEqual(
            permissions[collection]["fields_by_action"]["update"]["record_key"],
            ["id"],
        )

    def test_write_actions_have_exact_minimal_request_and_response_fields(self):
        worker = _load_contract()["worker_role"]["collections"]
        expected = {
            ("energy_forecast_latest", "updateOrCreate"): {
                "read": ["id", "station_id", "as_of", "content_hash", "updated_at"],
                "write": [
                    "station_id",
                    "as_of",
                    "generated_at",
                    "source_data_end",
                    "status",
                    "series_payload",
                    "model_manifest",
                    "content_hash",
                ],
            },
            ("energy_forecast_batches", "firstOrCreate"): {
                "read": [
                    "id",
                    "station_id",
                    "acceptance_run_id",
                    "issued_at",
                    "forecast_start_time",
                    "forecast_end_time",
                    "status",
                    "write_state",
                    "model_manifest",
                    "point_templates",
                    "content_hash",
                ],
                "write": [
                    "station_id",
                    "acceptance_run_id",
                    "issued_at",
                    "forecast_start_time",
                    "forecast_end_time",
                    "status",
                    "write_state",
                    "model_manifest",
                    "point_templates",
                    "content_hash",
                ],
            },
            ("energy_forecast_batches", "update"): {
                "read": [
                    "id",
                    "station_id",
                    "acceptance_run_id",
                    "issued_at",
                    "forecast_start_time",
                    "forecast_end_time",
                    "status",
                    "write_state",
                    "model_manifest",
                    "point_templates",
                    "content_hash",
                ],
                "write": ["write_state"],
            },
            ("energy_forecast_points", "firstOrCreate"): {
                "read": ["id", "batch_id", *POINT_HASH_FIELDS],
                "write": ["batch_id", *POINT_HASH_FIELDS],
            },
            ("energy_forecast_points", "update"): {
                "read": [
                    "id",
                    "unique_id",
                    "data_time",
                    "forecast_value",
                    "actual_value",
                    "actual_quality",
                    "actual_source_revision",
                    "actual_recorded_at",
                    "evaluated_at",
                    "absolute_percentage_error",
                ],
                "write": [
                    "actual_value",
                    "actual_quality",
                    "actual_source_revision",
                    "actual_recorded_at",
                    "evaluated_at",
                    "absolute_percentage_error",
                ],
            },
            ("energy_forecast_evaluations", "updateOrCreate"): {
                "read": [
                    "id",
                    "station_id",
                    "acceptance_run_id",
                    "evaluation_key",
                    "window_start",
                    "window_end",
                    "expected_count",
                    "valid_count",
                    "zero_actual_count",
                    *METRIC_FIELDS,
                    "outcome",
                    "calculated_at",
                ],
                "write": [
                    "station_id",
                    "acceptance_run_id",
                    "evaluation_key",
                    "window_start",
                    "window_end",
                    "expected_count",
                    "valid_count",
                    "zero_actual_count",
                    *METRIC_FIELDS,
                    "outcome",
                    "calculated_at",
                ],
            },
        }
        for (collection, action), fields in expected.items():
            declared = worker[collection]["fields_by_action"][action]
            self.assertEqual(declared["read"], fields["read"])
            self.assertEqual(declared["write"], fields["write"])

    def test_acl_filtered_latest_and_batch_responses_satisfy_task7_validators(self):
        permissions = _load_contract()["worker_role"]["collections"]
        latest_reads = permissions["energy_forecast_latest"]["fields_by_action"][
            "updateOrCreate"
        ]["read"]
        smoke = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))["values"]
        domain = dict(smoke)
        domain["series"] = domain.pop("series_payload")
        snapshot = LatestSnapshot.model_validate(domain)

        class LatestApi:
            def __init__(self, missing=None):
                self.missing = missing

            def list_records(self, collection, *, filter, fields, sort=None):
                return []

            def update_or_create(self, collection, filter, values):
                row = {
                    "id": 1,
                    **values,
                    "updated_at": "2026-08-25T01:17:06+08:00",
                }
                return {
                    field: row[field]
                    for field in latest_reads
                    if field != self.missing
                }

        ForecastSink(LatestApi()).publish_latest(snapshot)
        for missing in ("id", "station_id", "as_of", "content_hash"):
            with self.subTest(latest_missing=missing), self.assertRaises(M3Error):
                ForecastSink(LatestApi(missing)).publish_latest(snapshot)

        batch, _ = make_acceptance_records()
        batch_row = {"id": 10, **batch, "write_state": "writing"}
        sink = ForecastSink(object())
        for action in ("firstOrCreate", "update"):
            read_fields = permissions["energy_forecast_batches"]["fields_by_action"][
                action
            ]["read"]
            filtered = {field: batch_row[field] for field in read_fields}
            sink._validate_batch_response(filtered, batch)
            for missing in read_fields:
                with self.subTest(batch_action=action, missing=missing), self.assertRaises(
                    M3Error
                ):
                    sink._validate_batch_response(
                        {field: value for field, value in filtered.items() if field != missing},
                        batch,
                    )

    def test_acl_filtered_point_and_evaluation_responses_satisfy_task7_and_9(self):
        permissions = _load_contract()["worker_role"]["collections"]
        batch_reads = permissions["energy_forecast_batches"]["fields_by_action"]
        point_reads = permissions["energy_forecast_points"]["fields_by_action"][
            "firstOrCreate"
        ]["read"]

        class FilteredApi(FakeNocoBase):
            def __init__(self, missing_point=None):
                super().__init__()
                self.missing_point = missing_point

            @staticmethod
            def _fields(row, fields, missing=None):
                return {
                    field: row[field]
                    for field in fields
                    if field != missing
                }

            def first_or_create(self, collection, filter, values):
                row = super().first_or_create(collection, filter, values)
                if collection == "energy_forecast_batches":
                    fields = batch_reads["firstOrCreate"]["read"]
                    return self._fields(row, fields)
                return self._fields(row, point_reads, self.missing_point)

            def update_record(self, collection, record_id, values):
                row = super().update_record(collection, record_id, values)
                return self._fields(row, batch_reads["update"]["read"])

        batch, points = make_acceptance_records()
        ForecastSink(FilteredApi()).publish_acceptance(batch, points)
        for missing in point_reads:
            with self.subTest(point_missing=missing), self.assertRaises(M3Error):
                ForecastSink(FilteredApi(missing)).publish_acceptance(batch, points)

        point_update_reads = permissions["energy_forecast_points"]["fields_by_action"][
            "update"
        ]["read"]
        recorded_at = "2026-08-25T02:00:05+08:00"
        stored = {
            "id": 21,
            "unique_id": "station_total_load",
            "data_time": "2026-08-25T01:45:00+08:00",
            "data_time_parsed": datetime.fromisoformat("2026-08-25T01:45:00+08:00"),
            "forecast_value": 100.0,
            "forecast_value_parsed": 100.0,
        }
        actual = ObservationPoint(
            unique_id="station_total_load",
            ds=datetime.fromisoformat("2026-08-25T01:45:00+08:00"),
            y=90.0,
            quality="valid",
            source_revision=3,
        )
        actual_values = {
            "actual_value": 90.0,
            "actual_quality": "valid",
            "actual_source_revision": 3,
            "actual_recorded_at": recorded_at,
            "evaluated_at": recorded_at,
            "absolute_percentage_error": 100 * 10 / 90,
        }
        point_response = {
            "id": 21,
            "unique_id": "station_total_load",
            "data_time": "2026-08-25T01:45:00+08:00",
            "forecast_value": 100.0,
            **actual_values,
        }
        point_filtered = {field: point_response[field] for field in point_update_reads}
        AcceptanceService._validate_actual_update_response(
            point_filtered, stored, actual, actual_values
        )
        for missing in point_update_reads:
            with self.subTest(actual_missing=missing), self.assertRaises(M3Error):
                AcceptanceService._validate_actual_update_response(
                    {
                        field: value
                        for field, value in point_filtered.items()
                        if field != missing
                    },
                    stored,
                    actual,
                    actual_values,
                )

        evaluation_reads = permissions["energy_forecast_evaluations"][
            "fields_by_action"
        ]["updateOrCreate"]["read"]
        evaluation = {
            "station_id": "station-1",
            "acceptance_run_id": "run-20260825",
            "evaluation_key": "station_total_load",
            "window_start": "2026-08-25T01:00:00+08:00",
            "window_end": "2026-09-01T01:00:00+08:00",
            "expected_count": 672,
            "valid_count": 650,
            "zero_actual_count": 2,
            "mape_percent": 10.0,
            "mae": 5.0,
            "smape_percent": 9.5,
            "wape_percent": 9.0,
            "median_ape_percent": 8.5,
            "p90_ape_percent": 12.5,
            "outcome": "passed",
            "calculated_at": "2026-09-01T01:02:00+08:00",
        }
        evaluation_response = {"id": 31, **evaluation}
        evaluation_filtered = {
            field: evaluation_response[field] for field in evaluation_reads
        }
        AcceptanceService._validate_evaluation_response(
            evaluation_filtered, evaluation
        )
        for missing in evaluation_reads:
            with self.subTest(evaluation_missing=missing), self.assertRaises(M3Error):
                AcceptanceService._validate_evaluation_response(
                    {
                        field: value
                        for field, value in evaluation_filtered.items()
                        if field != missing
                    },
                    evaluation,
                )

    def test_dashboard_is_read_only_field_filtered_and_ems_scoped(self):
        contract = _load_contract()
        dashboard = contract["dashboard_role"]
        self.assertEqual(dashboard["allowed_actions"], ["list"])
        self.assertEqual(dashboard["denied_actions"], ["get", "create", "update", "updateOrCreate", "firstOrCreate", *DENIED_ACTIONS])
        self.assertEqual(dashboard["hidden_fields"], {})
        self.assertEqual(set(dashboard["collections"]), set(DASHBOARD_LIST_MATRIX))
        for name, item in dashboard["collections"].items():
            self.assertEqual(item["allowed_actions"], ["list"], name)
            self.assertEqual(set(item["fields_by_action"]), {"list"}, name)
            self.assertEqual(
                item["fields_by_action"]["list"], DASHBOARD_LIST_MATRIX[name], name
            )

        gate = dashboard["station_scope_gate"]
        self.assertTrue(gate["required_before_production"])
        self.assertEqual(
            gate["enforced_by"],
            "node_red_nocobase_auth_check_and_fixed_server_bindings",
        )
        self.assertEqual(
            gate["visibility"], "all_authenticated_iframe_users_see_both_stations"
        )
        self.assertEqual(gate["station_id_source"], "configured_server_bindings")
        self.assertFalse(gate["allow_client_supplied_station_override"])
        self.assertEqual(gate["credential_exposure"], "server_side_only")
        self.assertEqual(
            gate["evidence_required"],
            [
                "authenticated_nocobase_user_positive",
                "unauthenticated_and_expired_token_denied_before_exec",
                "client_station_and_target_override_denied",
                "fixed_query_matrix_cannot_be_influenced_by_public_input",
            ],
        )

        documentation = dashboard["installed_api_documentation"]
        self.assertEqual(documentation["gate"], "hard_pre_production")
        self.assertEqual(
            documentation["evidence_required"],
            [
                "dashboard_list_action_method_params_and_response_projection",
                "independent_read_filter_sort_write_record_key_permissions",
                "exact_dashboard_list_field_filter_and_sort_permissions",
                "dashboard_key_has_no_get_or_write_actions",
            ],
        )
        self.assertEqual(
            documentation["if_unsupported"],
            "stop_update_machine_contract_and_tests_then_rereview",
        )

    @unittest.skip(
        "legacy Node-RED flow is retained but not deployed; current Flow is verified onsite"
    )
    def test_dashboard_list_acl_exactly_matches_task12_fixed_flow_queries(self):
        contract = _load_contract()
        dashboard = contract["dashboard_role"]
        declared = {
            collection: item["fields_by_action"]["list"]
            for collection, item in dashboard["collections"].items()
        }
        self.assertEqual(declared, DASHBOARD_LIST_MATRIX)

        flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
        functions = "\n".join(
            node["func"] for node in flow if node.get("type") == "function"
        )
        requests = [
            node
            for node in flow
            if node.get("type") == "http request"
            and "/api/energy_forecast_" in node.get("url", "")
        ]
        self.assertEqual(len(requests), 6)
        actual = {}
        point_series = []
        seen_collection_actions = set()
        for node in requests:
            url = node["url"]
            match = re.search(r"/api/([a-z_]+):(\w+)\?", url)
            self.assertIsNotNone(match, node["name"])
            collection, action = match.groups()
            self.assertEqual(action, "list")
            seen_collection_actions.add((collection, action))
            self.assertEqual(node["method"], "GET")
            self.assertTrue(url.startswith("${M3_NOCOBASE_BASE_URL}/api/"))
            self.assertNotIn("msg.req", url)
            self.assertNotIn("{{{url", url)
            query = parse_qs(urlsplit(url).query)
            read = query["fields"][0].split(",")
            sort = [
                field.lstrip("-")
                for field in query.get("sort", [""])[0].split(",")
                if field
            ]
            if collection == "energy_forecast_batches":
                self.assertEqual(query["sort"], ["-issued_at"])
            if collection == "energy_forecast_points":
                self.assertEqual(query["sort"], ["unique_id,data_time"])
            if collection == "energy_forecast_points":
                self.assertEqual(query["page"], ["1"])
                self.assertEqual(query["pageSize"], ["672"])
                filter_body = json.loads(query["filter"][0])
                self.assertEqual(
                    filter_body,
                    {
                        "batch.station_id": "{{{m3.pointFilterStation}}}",
                        "batch.acceptance_run_id": "{{{m3.pointFilterRun}}}",
                        "batch.write_state": "complete",
                        "unique_id": filter_body["unique_id"],
                    },
                )
                self.assertIn(
                    filter_body["unique_id"],
                    ("station_total_load", "storage_1_soc", "storage_2_soc"),
                )
                point_series.append(filter_body["unique_id"])
                filter_fields = list(filter_body)
            else:
                filter_variable = re.fullmatch(
                    r"\{\{\{m3\.([A-Za-z]+)\}\}\}", query["filter"][0]
                )
                self.assertIsNotNone(filter_variable, node["name"])
                variable = filter_variable.group(1)
                assignment = re.search(
                    rf"msg\.m3\.{variable}\s*=\s*encodeURIComponent\(JSON\.stringify\(\{{(.*?)\}}\)\);",
                    functions,
                )
                self.assertIsNotNone(assignment, variable)
                filter_fields = [
                    quoted or bare
                    for quoted, bare in re.findall(
                        r'(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))\s*:',
                        assignment.group(1),
                    )
                ]
            request_contract = {
                "read": read,
                "filter": filter_fields,
                "sort": sort,
                "write": [],
                "record_key": [],
            }
            self.assertEqual(request_contract, DASHBOARD_LIST_MATRIX[collection])
            actual[collection] = request_contract
        self.assertEqual(actual, DASHBOARD_LIST_MATRIX)
        self.assertEqual(
            seen_collection_actions,
            {(collection, "list") for collection in DASHBOARD_LIST_MATRIX},
        )
        self.assertEqual(
            point_series,
            ["station_total_load", "storage_1_soc", "storage_2_soc"],
        )
        self.assertEqual(len(set(point_series)), 3)

        policy = dashboard["fixed_server_query_policy"]
        self.assertEqual(
            policy,
            {
                "client_influence": {
                    "action": False,
                    "collection": False,
                    "target": False,
                    "filter": False,
                    "sort": False,
                },
                "value_sources": {
                    "station_id": "authenticated_ems_scope",
                    "write_state": "constant_complete",
                    "acceptance_run_id": "newest_authorized_complete_batch",
                    "sort": "fixed_server_constants",
                },
            },
        )
        self.assertIn(
            'const stationId = c.validateDashboardAuthorization(msg.payload);',
            functions,
        )
        self.assertIn('write_state: "complete"', functions)
        self.assertIn(
            "const selected = c.selectAcceptanceBatches(rows, msg.m3.stationId);",
            functions,
        )
        self.assertIn(
            "msg.m3.pointFilterStation = encodeURIComponent(msg.m3.stationId);",
            functions,
        )
        self.assertIn(
            "msg.m3.pointFilterRun = encodeURIComponent(selected.runId);",
            functions,
        )
        self.assertIn(
            'keys.some((key) => key !== "token")',
            NODE_CONTRACT_PATH.read_text(encoding="utf-8"),
        )
        self.assertIn("delete msg.url;", NODE_CONTRACT_PATH.read_text(encoding="utf-8"))
        for forbidden in (
            "msg.req.query.station_id",
            "msg.req.query.url",
            "msg.req.body.collection",
            "msg.req.body.action",
            "msg.req.body.filter",
            "msg.req.body.sort",
        ):
            self.assertNotIn(forbidden, functions)

    def test_roles_use_only_the_declared_action_vocabulary(self):
        contract = _load_contract()
        vocabulary = set(contract["action_vocabulary"])
        self.assertEqual(
            vocabulary,
            {"list", "get", "create", "update", "updateOrCreate", "firstOrCreate", "destroy", "delete", "export", "import"},
        )
        for role_name in ("worker_role", "dashboard_role"):
            role = contract[role_name]
            self.assertEqual(role["unlisted_actions"], "deny", role_name)
            self.assertEqual(role["unlisted_fields"], "deny", role_name)
            for channel in (
                "unlisted_read_fields",
                "unlisted_filter_fields",
                "unlisted_sort_fields",
                "unlisted_write_fields",
                "unlisted_record_key_fields",
            ):
                self.assertEqual(role[channel], "deny", (role_name, channel))
            self.assertTrue(set(role["allowed_actions"]) <= vocabulary)
            self.assertTrue(set(role["denied_actions"]) <= vocabulary)
            for collection_name, item in role["collections"].items():
                self.assertTrue(set(item["allowed_actions"]) <= vocabulary)
                self.assertTrue(set(item["denied_actions"]) <= vocabulary)
                self.assertTrue(set(item["fields_by_action"]) <= vocabulary)
                for fields in item["fields_by_action"].values():
                    self.assertEqual(
                        set(fields), {"read", "filter", "sort", "write", "record_key"}
                    )
                    self.assertTrue(
                        set(fields.get("read", [])) <= set(COLLECTION_FIELDS[collection_name]),
                        (role_name, collection_name),
                    )
                    relationship = contract["relationships"][0]
                    relation_filters = (
                        set(relationship["required_dotted_filters"])
                        if collection_name == relationship["source_collection"]
                        else set()
                    )
                    self.assertTrue(
                        set(fields.get("filter", []))
                        <= set(COLLECTION_FIELDS[collection_name]) | relation_filters,
                        (role_name, collection_name),
                    )
                    self.assertTrue(
                        set(fields.get("sort", []))
                        <= set(COLLECTION_FIELDS[collection_name]),
                        (role_name, collection_name),
                    )
                    self.assertTrue(
                        set(fields.get("write", [])) <= set(COLLECTION_FIELDS[collection_name]),
                        (role_name, collection_name),
                    )
                    self.assertTrue(
                        set(fields.get("record_key", []))
                        <= set(COLLECTION_FIELDS[collection_name]),
                        (role_name, collection_name),
                    )

        relation = contract["optional_station_relation"]
        self.assertFalse(relation["configured"])
        self.assertIsNone(relation["target_collection"])
        self.assertIsNone(relation["target_field"])
        self.assertEqual(relation["relation_specific_indexes"], [])
        self.assertTrue(relation["when_configured"]["index_required"])

    def test_latest_smoke_record_is_exact_safe_json_without_secrets(self):
        record_text = SMOKE_PATH.read_text(encoding="utf-8")
        record = json.loads(record_text)
        self.assertEqual(set(record), {"filter", "values"})
        self.assertEqual(record["filter"], {"station_id": "m3-contract-smoke"})
        self.assertEqual(record["values"]["station_id"], "m3-contract-smoke")
        self.assertEqual(
            set(record["values"]),
            set(COLLECTION_FIELDS["energy_forecast_latest"]) - {"id", "updated_at"},
        )
        self.assertNotIn("updated_at", record["values"])
        self.assertEqual(record["values"]["status"], "degraded")
        self.assertEqual(
            [series["unique_id"] for series in record["values"]["series_payload"]],
            ["station_total_load", "storage_soc"],
        )
        for series in record["values"]["series_payload"]:
            self.assertEqual(series["model_name"], "none")
            self.assertEqual(series["status"], "error")
            self.assertEqual(series["points"], [])
            self.assertEqual(series["fallback_reason"], "contract_smoke")
        for field in ("as_of", "generated_at", "source_data_end"):
            parsed = datetime.fromisoformat(record["values"][field])
            self.assertIsNotNone(parsed.utcoffset(), field)
        self.assertRegex(record["values"]["content_hash"], r"^[0-9a-f]{64}$")
        self.assertNotRegex(record_text, r"(?i)(api[_-]?key|token|password|secret)\s*[\":=]+\s*[\"']?(?!\$\{)")

    def test_smoke_digest_uses_the_production_domain_canonical_hash(self):
        record = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
        domain_body = dict(record["values"])
        supplied_digest = domain_body.pop("content_hash")
        domain_body["series"] = domain_body.pop("series_payload")

        self.assertEqual(supplied_digest, canonical_hash(domain_body))

    def test_task7_latest_payload_matches_only_contract_client_write_fields(self):
        class CapturingApi:
            def __init__(self):
                self.values = None

            def list_records(self, collection, *, filter, fields, sort=None):
                self.assertion = (collection, filter, fields, sort)
                return []

            def update_or_create(self, collection, filter, values):
                self.values = dict(values)
                return {
                    "id": 1,
                    **values,
                    "updated_at": "2026-08-25T01:17:06+08:00",
                }

        record = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
        domain = dict(record["values"])
        domain.pop("updated_at", None)
        domain["series"] = domain.pop("series_payload")
        snapshot = LatestSnapshot.model_validate(domain)
        api = CapturingApi()

        ForecastSink(api).publish_latest(snapshot)

        contract = _load_contract()
        expected = contract["worker_role"]["collections"]["energy_forecast_latest"][
            "fields_by_action"
        ]["updateOrCreate"]["write"]
        self.assertEqual(set(api.values), set(expected))
        self.assertNotIn("updated_at", api.values)

    @unittest.skip(
        "superseded pre-systemd NocoBase smoke guide; current deployment guide has dedicated tests"
    )
    def test_deployment_guide_is_an_operator_verifiable_http_only_gate(self):
        guide = GUIDE_PATH.read_text(encoding="utf-8")
        required = [
            "NocoBase API Documentation",
            "生产前阻断门",
            "m3/contracts/nocobase_collections.json",
            "四个集合",
            "唯一约束",
            "检查约束",
            "外键",
            "索引",
            "字段权限",
            "API Key",
            "Authorization: Bearer ${M3_NOCOBASE_API_KEY}",
            "${M3_NOCOBASE_BASE_URL}/api/energy_forecast_latest:list?filter=",
            "${M3_NOCOBASE_BASE_URL}/api/energy_forecast_latest:updateOrCreate",
            "--data-binary @m3/contracts/latest-smoke-record.json",
            "filterByTk=0",
            "M3_NOCOBASE_ADMIN_API_KEY",
            "updated_at",
            "服务端在 create/update 时生成",
            "默认不授予",
            "修改机器契约、测试并重新复审",
            "energy_forecast_points.batch_id",
            "belongs-to",
            "关联别名 `batch`",
            "batch.station_id",
            "batch.acceptance_run_id",
            "batch.write_state",
            "M3_RELATION_FILTER_PROBE",
            "read-only synthetic relation-filter probe",
            "不得改走其他查询路径",
            "M3_SMOKE_PREFLIGHT",
            "filter=%7B%22station_id%22%3A%22m3-contract-smoke%22%7D",
            '"data":[],"meta":{"count":0,"page":1,"pageSize":1,"totalPage":0}',
            "管理员身份核验清理流程",
            "回到本节预检起点",
            "M3_NOCOBASE_TIMESTAMP_PRECISION_SECONDS",
            'type(first["id"]) is int',
            'first["id"] == second["id"]',
            "/internal/energy-forecast/v1/alerts",
            '{"status":"ok"}',
            "HTTP",
        ]
        for marker in required:
            self.assertIn(marker, guide, marker)
        self.assertNotRegex(
            guide,
            r"(?i)(postgresql://|postgres://|\bpsql\b|\bselect\s+.+\s+from\b|\binsert\s+into\b|\bupdate\s+[a-z_][a-z0-9_]*\s+set\b|\bdelete\s+from\b)",
        )
        self.assertNotRegex(guide, r"Authorization: Bearer (?!\$\{)")
        cleanup = guide.split("## 8. 管理员清理烟雾记录", 1)[1].split("## 9.", 1)[0]
        self.assertIn("Authorization: Bearer ${M3_NOCOBASE_ADMIN_API_KEY}", cleanup)
        self.assertNotIn("Authorization: Bearer ${M3_NOCOBASE_API_KEY}", cleanup)
        self.assertIn("不得使用 Worker Key", cleanup)
        probes = guide.split("## 6. Worker Key 允许与拒绝探针", 1)[1].split(
            "## 7.", 1
        )[0]
        first_upsert = probes.index("M3_SMOKE_RESPONSE_ONE=")
        self.assertLess(probes.index("M3_SMOKE_PREFLIGHT="), first_upsert)
        self.assertNotIn(":destroy", probes[:first_upsert])
        self.assertEqual(
            probes.count(
                '${M3_NOCOBASE_BASE_URL}/api/energy_forecast_latest:updateOrCreate"'
            ),
            2,
        )

        relation_script = _guide_python_script("M3_RELATION_FILTER_PROBE")
        relation_empty = {
            "data": [],
            "meta": {"count": 0, "page": 1, "pageSize": 1, "totalPage": 0},
        }
        with patch.dict(
            os.environ,
            {"M3_RELATION_FILTER_PROBE": json.dumps(relation_empty)},
            clear=False,
        ):
            exec(relation_script, {})
        with patch.dict(
            os.environ,
            {
                "M3_RELATION_FILTER_PROBE": json.dumps(
                    {**relation_empty, "unexpected": True}
                )
            },
            clear=False,
        ), self.assertRaises(AssertionError):
            exec(relation_script, {})
        for field, expected in relation_empty["meta"].items():
            for invalid in (bool(expected), float(expected)):
                payload = {
                    "data": [],
                    "meta": {**relation_empty["meta"], field: invalid},
                }
                with self.subTest(
                    relation_meta_field=field, invalid=invalid
                ), patch.dict(
                    os.environ,
                    {"M3_RELATION_FILTER_PROBE": json.dumps(payload)},
                    clear=False,
                ), self.assertRaises(AssertionError):
                    exec(relation_script, {})

    @unittest.skip(
        "superseded legacy Dashboard query matrix; current CLI ACL has dedicated tests"
    )
    def test_deployment_guide_closes_task12_dashboard_acl_gate_exactly(self):
        guide = GUIDE_PATH.read_text(encoding="utf-8")
        required = [
            "Task 12 Dashboard list 精确矩阵",
            "latest list",
            "station_id/as_of/generated_at/source_data_end/status/series_payload/model_manifest/content_hash/updated_at",
            "batches list",
            "id/station_id/acceptance_run_id/issued_at/write_state",
            "points list",
            "batch_id/unique_id/data_time/actual_quality",
            "filter 仅 `batch.station_id/batch.acceptance_run_id/batch.write_state/unique_id`",
            "6 个固定 NocoBase request 节点",
            "page=1/pageSize=672",
            "station_total_load",
            "storage_1_soc",
            "storage_2_soc",
            "evaluations list",
            "station_id/acceptance_run_id/evaluation_key/expected_count/valid_count/zero_actual_count/mape_percent/mae/smape_percent/outcome",
            "newest authorized complete batch",
            "write_state=complete",
            "action/collection/target/filter/sort",
            "get 保持只读",
            "record-key 仍仅为 `id`",
            "cross-tenant",
            "NocoBase API Documentation",
            "响应投影",
            "该唯一 ACL delta 已通过独立复审：`Approved`，0 个 Critical/Important/Minor findings。",
            "Task 11 focused **19/19**",
            "完整 M3 **234/234**",
            "6 节点/4 action 固定查询扫描通过",
            "仅移除 points list filter 中的 `unique_id` 后，机器契约 SHA-256 与此前已批准版本完全一致",
        ]
        for marker in required:
            self.assertIn(marker, guide, marker)
        for stale_review_status in (
            "等待独立复审",
            "pending independent review",
            "独立复审签字前",
        ):
            self.assertNotIn(stale_review_status, guide, stale_review_status)
        self.assertNotIn(
            "list filter/sort allowlist 为空，尚不能执行本 flow",
            guide,
        )
        self.assertIn(
            "Task 11 Dashboard ACL 与 Task 12 固定查询矩阵已通过机器测试逐项一致性核验",
            guide,
        )

    @unittest.skip(
        "superseded destructive smoke-record workflow; current deployment uses read-only CLI smoke"
    )
    def test_deployment_smoke_validators_fail_closed_on_existing_or_partial_rows(self):
        preflight_script = _guide_python_script("M3_SMOKE_PREFLIGHT")
        empty_envelope = {
            "data": [],
            "meta": {"count": 0, "page": 1, "pageSize": 1, "totalPage": 0},
        }
        with patch.dict(
            os.environ,
            {"M3_SMOKE_PREFLIGHT": json.dumps(empty_envelope)},
            clear=False,
        ):
            exec(preflight_script, {})
        existing = {
            "data": [{"id": 1, "station_id": "m3-contract-smoke"}],
            "meta": {"count": 1, "page": 1, "pageSize": 1, "totalPage": 1},
        }
        with patch.dict(
            os.environ,
            {"M3_SMOKE_PREFLIGHT": json.dumps(existing)},
            clear=False,
        ), self.assertRaises(AssertionError):
            exec(preflight_script, {})
        invalid_preflights = [
            {**empty_envelope, "unexpected": True},
            {"data": []},
            {"data": {}, "meta": empty_envelope["meta"]},
            {"data": [], "meta": {**empty_envelope["meta"], "count": 1}},
            {"data": [], "meta": {**empty_envelope["meta"], "page": 2}},
            {"data": [], "meta": {**empty_envelope["meta"], "pageSize": 2}},
            {"data": [], "meta": {**empty_envelope["meta"], "totalPage": 1}},
            {"data": [], "meta": {**empty_envelope["meta"], "extra": 0}},
        ]
        for payload in invalid_preflights:
            with self.subTest(preflight=payload), patch.dict(
                os.environ,
                {"M3_SMOKE_PREFLIGHT": json.dumps(payload)},
                clear=False,
            ), self.assertRaises(AssertionError):
                exec(preflight_script, {})
        for field, expected in empty_envelope["meta"].items():
            for invalid in (bool(expected), float(expected)):
                payload = {
                    "data": [],
                    "meta": {**empty_envelope["meta"], field: invalid},
                }
                with self.subTest(
                    preflight_meta_field=field, invalid=invalid
                ), patch.dict(
                    os.environ,
                    {"M3_SMOKE_PREFLIGHT": json.dumps(payload)},
                    clear=False,
                ), self.assertRaises(AssertionError):
                    exec(preflight_script, {})

        response_script = _guide_python_script("M3_SMOKE_RESPONSE_ONE")
        first = {
            "id": 41,
            "station_id": "m3-contract-smoke",
            "as_of": "2026-08-25T01:17:00+08:00",
            "content_hash": "0cca46345ec92fc854caeecfdaaf73c0ae1956659bf36bf26ecf15b183035f44",
            "updated_at": "2026-08-25T01:17:06+08:00",
        }
        second = {**first, "updated_at": "2026-08-25T01:17:08+08:00"}

        def execute_envelopes(left_envelope, right_envelope):
            with patch.dict(
                os.environ,
                {
                    "M3_SMOKE_RESPONSE_ONE": json.dumps(left_envelope),
                    "M3_SMOKE_RESPONSE_TWO": json.dumps(right_envelope),
                },
                clear=False,
            ):
                exec(response_script, {})

        def execute_responses(left, right):
            execute_envelopes({"data": left}, {"data": right})

        execute_responses(first, second)
        for field in first:
            with self.subTest(first_missing=field), self.assertRaises(AssertionError):
                execute_responses(
                    {key: value for key, value in first.items() if key != field},
                    second,
                )
            with self.subTest(second_missing=field), self.assertRaises(AssertionError):
                execute_responses(
                    first,
                    {key: value for key, value in second.items() if key != field},
                )
        with self.assertRaises(AssertionError):
            execute_responses(first, {**second, "id": 42})
        for invalid_id in (True, False, 0, -1, 1.0, "41", None):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(AssertionError):
                execute_responses({**first, "id": invalid_id}, {**second, "id": invalid_id})
        for field, invalid in (
            ("station_id", "station-1"),
            ("as_of", "2026-08-25T01:18:00+08:00"),
            ("content_hash", "f" * 64),
        ):
            with self.subTest(identity_field=field), self.assertRaises(AssertionError):
                execute_responses({**first, field: invalid}, second)
            with self.subTest(second_identity_field=field), self.assertRaises(AssertionError):
                execute_responses(first, {**second, field: invalid})
        for first_time, second_time in (
            (first["updated_at"], first["updated_at"]),
            (second["updated_at"], first["updated_at"]),
            ("2026-08-25T01:17:06", second["updated_at"]),
            (first["updated_at"], "2026-08-25T01:17:08"),
        ):
            with self.subTest(times=(first_time, second_time)), self.assertRaises(
                AssertionError
            ):
                execute_responses(
                    {**first, "updated_at": first_time},
                    {**second, "updated_at": second_time},
                )
        with self.assertRaises(AssertionError):
            execute_envelopes({"data": first, "extra": 1}, {"data": second})
        with self.assertRaises(AssertionError):
            execute_envelopes({"data": first}, {"data": second, "extra": 1})
        with self.assertRaises(AssertionError):
            execute_responses({**first, "extra": 1}, second)
        with self.assertRaises(AssertionError):
            execute_responses(first, {**second, "extra": 1})


if __name__ == "__main__":
    unittest.main()
