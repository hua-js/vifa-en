import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from m4.orchestrator import load_station_input
from m4.tests.m4_orchestrator_test_support import make_request


class M4OrchestratorLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.temp_dir = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_json(self, payload: object, name: str = "station.json") -> Path:
        path = self.temp_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_text(self, text: str, name: str = "station.json") -> Path:
        path = self.temp_dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_json_returns_a_strict_request(self):
        path = self.write_json(make_request().model_dump(mode="json"))

        loaded = load_station_input(path)

        self.assertIsNotNone(loaded.request)
        self.assertIsNone(loaded.error)
        self.assertEqual(loaded.request.station_id, "station-test-001")
        self.assertEqual(loaded.input_ref, path.name)

    def test_json_and_validation_errors_are_distinct_and_safe(self):
        malformed = self.write_text("{not-json", "malformed.json")
        invalid = self.write_json(
            {"station_id": "station-visible", "request_id": "r"},
            "invalid.json",
        )

        malformed_result = load_station_input(malformed)
        invalid_result = load_station_input(invalid)

        self.assertEqual(malformed_result.error.code, "INPUT_JSON_ERROR")
        self.assertEqual(invalid_result.error.code, "INPUT_VALIDATION_ERROR")
        self.assertEqual(invalid_result.station_id_hint, "station-visible")
        self.assertEqual(invalid_result.request_id_hint, "r")
        for result in (malformed_result, invalid_result):
            rendered = result.model_dump_json()
            self.assertNotIn(str(self.temp_dir), rendered)
            self.assertNotIn("not-json", rendered)

    def test_json_recursion_error_returns_safe_json_error(self):
        path = self.write_text(
            '"SENSITIVE_DEEP_JSON_BODY"',
            "private-deep-input.json",
        )
        recursion_error = RecursionError(
            "SENSITIVE_RECURSION_DETAIL /private/deep-input.json"
        )

        with patch(
            "m4.orchestrator.loader.json.loads",
            side_effect=recursion_error,
        ):
            result = load_station_input(path, input_ref="input-deep")

        self.assertIsNone(result.request)
        self.assertEqual(result.error.code, "INPUT_JSON_ERROR")
        self.assertEqual(result.error.message, "input file is not valid JSON")
        rendered = result.model_dump_json()
        for sensitive_value in (
            str(path),
            "private-deep-input.json",
            "SENSITIVE_DEEP_JSON_BODY",
            "SENSITIVE_RECURSION_DETAIL",
            "/private/deep-input.json",
            "RecursionError",
        ):
            self.assertNotIn(sensitive_value, rendered)

    def test_json_loader_does_not_map_memory_error_or_base_exceptions(self):
        path = self.write_text("{}", "resource-boundary.json")
        failures = (
            MemoryError("MEMORY_BOUNDARY"),
            KeyboardInterrupt("INTERRUPT_BOUNDARY"),
            SystemExit(37),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "m4.orchestrator.loader.json.loads",
                    side_effect=failure,
                ):
                    with self.assertRaises(type(failure)) as raised:
                        load_station_input(path, input_ref="input-boundary")

                self.assertIs(raised.exception, failure)

    def test_not_found_returns_safe_error(self):
        missing = self.temp_dir / "missing.json"

        result = load_station_input(missing)

        self.assertEqual(result.error.code, "INPUT_NOT_FOUND")
        self.assertEqual(result.error.message, "input file was not found")
        self.assertEqual(result.input_ref, missing.name)

    def test_read_error_returns_safe_error(self):
        path = self.temp_dir / "unreadable.json"
        with patch.object(Path, "read_text", side_effect=PermissionError):
            result = load_station_input(path)

        self.assertEqual(result.error.code, "INPUT_READ_ERROR")
        self.assertEqual(result.error.message, "input file could not be read")

    def test_invalid_utf8_returns_safe_error(self):
        path = self.temp_dir / "invalid-encoding.json"
        path.write_bytes(b"\xff")

        result = load_station_input(path)

        self.assertEqual(result.error.code, "INPUT_ENCODING_ERROR")
        self.assertEqual(result.error.message, "input file is not valid UTF-8")

    def test_caller_provided_safe_input_ref_is_preserved(self):
        path = self.write_json(make_request().model_dump(mode="json"))

        result = load_station_input(path, input_ref="station-visible.json")

        self.assertEqual(result.input_ref, "station-visible.json")

    def test_unsafe_input_ref_is_rejected_by_the_contract(self):
        path = self.write_json(make_request().model_dump(mode="json"))

        for input_ref in ("inputs/station.json", "inputs\\station.json", "station\n.json"):
            with self.subTest(input_ref=input_ref):
                with self.assertRaises(ValidationError):
                    load_station_input(path, input_ref=input_ref)

    def test_blank_input_ref_is_rejected(self):
        path = self.write_json(make_request().model_dump(mode="json"))

        with self.assertRaises(ValueError):
            load_station_input(path, input_ref="   ")

    def test_hint_extraction_requires_nonblank_strings(self):
        path = self.write_json(
            {"station_id": "   ", "request_id": 12}, "bad-hints.json"
        )

        result = load_station_input(path)

        self.assertEqual(result.error.code, "INPUT_VALIDATION_ERROR")
        self.assertIsNone(result.station_id_hint)
        self.assertIsNone(result.request_id_hint)

    def test_non_contract_week_date_timestamps_are_rejected(self):
        payload = make_request().model_dump(mode="json")

        def week_date(timestamp: str) -> str:
            return timestamp.replace("2026-09-04", "2026-W36-5").replace(
                "+08:00", "+0800"
            )

        payload["plan_start_at"] = week_date(payload["plan_start_at"])
        payload["input_observed_at"] = week_date(payload["input_observed_at"])
        self.assertEqual(len(payload["points"]), 96)
        for point in payload["points"]:
            point["timestamp"] = week_date(point["timestamp"])
        path = self.write_json(payload, "non-contract-timestamps.json")

        result = load_station_input(path)

        self.assertIsNone(result.request)
        self.assertEqual(result.error.code, "INPUT_VALIDATION_ERROR")

    def test_all_input_errors_redact_input_and_exception_details(self):
        secret_json = "SECRET_JSON_BODY"
        missing = self.temp_dir / "missing.json"
        malformed = self.write_text(f"{{not-json {secret_json}", "malformed.json")
        invalid = self.write_json({"unexpected": secret_json}, "invalid.json")
        invalid_encoding = self.temp_dir / "invalid-encoding.json"
        invalid_encoding.write_bytes(b"\xffSECRET_JSON_BODY")
        unreadable = self.temp_dir / "unreadable.json"

        with patch.object(
            Path,
            "read_text",
            side_effect=PermissionError("SECRET_EXCEPTION /private/secret TRACEBACK"),
        ):
            read_error = load_station_input(unreadable)

        results = [
            load_station_input(missing),
            load_station_input(invalid_encoding),
            load_station_input(malformed),
            load_station_input(invalid),
            read_error,
        ]

        self.assertEqual(
            [result.error.code for result in results],
            [
                "INPUT_NOT_FOUND",
                "INPUT_ENCODING_ERROR",
                "INPUT_JSON_ERROR",
                "INPUT_VALIDATION_ERROR",
                "INPUT_READ_ERROR",
            ],
        )
        for result in results:
            rendered = result.model_dump_json()
            for sensitive_value in (
                str(self.temp_dir),
                secret_json,
                "SECRET_EXCEPTION",
                "/private/secret",
                "TRACEBACK",
            ):
                self.assertNotIn(sensitive_value, rendered)


if __name__ == "__main__":
    unittest.main()
