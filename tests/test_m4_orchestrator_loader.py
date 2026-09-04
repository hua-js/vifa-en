import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from m4_orchestrator import load_station_input
from tests.m4_orchestrator_test_support import make_request


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


if __name__ == "__main__":
    unittest.main()
