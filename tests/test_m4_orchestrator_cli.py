import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from m4_orchestrator import M4OrchestrationResult, M4Orchestrator, StationInput
from m4_orchestrator.cli import main
from m4_orchestrator.writer import (
    OutputWriteError,
    serialize_result,
    write_result_atomic,
)
from tests.m4_optimizer_test_support import make_request
from tests.m4_orchestrator_test_support import (
    DeterministicOptimizer,
    FIXED_FINISHED_AT,
    FIXED_STARTED_AT,
)


def make_orchestration_result() -> M4OrchestrationResult:
    times = iter((FIXED_STARTED_AT, FIXED_FINISHED_AT))
    return M4Orchestrator(
        model_version="test-model-v1",
        orchestrator_version="test-orchestrator-v1",
        optimizer=DeterministicOptimizer(),
        clock=lambda: next(times),
        run_id_factory=lambda: "run-test-001",
    ).run(
        [
            StationInput(
                input_ref="input-1",
                request=make_request(
                    station_id="电站-1",
                    request_id="request-station-1",
                ),
            )
        ]
    )


class M4OrchestratorWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.temp_dir = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_serialization_is_deterministic_strict_utf8_json_with_newline(self):
        result = make_orchestration_result()

        first = serialize_result(result)
        second = serialize_result(result)

        expected = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertTrue(first.endswith("\n"))
        self.assertFalse(first.endswith("\n\n"))
        self.assertIn("电站-1", first)
        self.assertNotIn("\\u7535", first)
        parsed = M4OrchestrationResult.model_validate_json(first)
        self.assertEqual(parsed, result)

    def test_atomic_write_replaces_target_with_parseable_result(self):
        target = self.temp_dir / "result.json"
        target.write_text("old-result\n", encoding="utf-8")
        result = make_orchestration_result()

        write_result_atomic(result, target)

        text = target.read_text(encoding="utf-8")
        self.assertEqual(text, serialize_result(result))
        self.assertEqual(M4OrchestrationResult.model_validate_json(text), result)
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_replace_failure_preserves_existing_target_and_removes_temp_file(self):
        target = self.temp_dir / "result.json"
        target.write_text("old-result\n", encoding="utf-8")
        with patch(
            "m4_orchestrator.writer.os.replace",
            side_effect=OSError("blocked"),
        ):
            with self.assertRaisesRegex(
                OutputWriteError,
                "^orchestration result could not be written$",
            ):
                write_result_atomic(make_orchestration_result(), target)

        self.assertEqual(target.read_text(encoding="utf-8"), "old-result\n")
        self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


class M4OrchestratorCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.temp_dir = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_request(
        self,
        station_id: str,
        *,
        request_id: str | None = None,
        name: str | None = None,
    ) -> Path:
        path = self.temp_dir / (name or f"{station_id}.json")
        request = make_request(
            station_id=station_id,
            request_id=request_id or f"request-{station_id}",
        )
        path.write_text(request.model_dump_json(), encoding="utf-8")
        return path

    def invoke(
        self,
        inputs: list[Path],
        output: Path,
        *,
        optimizer: DeterministicOptimizer | None = None,
        orchestrator_version: str | None = None,
    ) -> tuple[int, str, str, DeterministicOptimizer]:
        actual_optimizer = optimizer or DeterministicOptimizer()
        argv = []
        for input_path in inputs:
            argv.extend(("--input", str(input_path)))
        argv.extend(
            (
                "--output",
                str(output),
                "--model-version",
                "cli-model-v1",
            )
        )
        if orchestrator_version is not None:
            argv.extend(("--orchestrator-version", orchestrator_version))

        stdout = StringIO()
        stderr = StringIO()
        with patch(
            "m4_orchestrator.service.M4Optimizer",
            return_value=actual_optimizer,
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue(), actual_optimizer

    def read_result(self, output: Path) -> M4OrchestrationResult:
        return M4OrchestrationResult.model_validate_json(
            output.read_text(encoding="utf-8")
        )

    def test_two_valid_inputs_write_completed_result_and_return_zero(self):
        first = self.write_request("station-2")
        second = self.write_request("station-1")
        output = self.temp_dir / "results" / "result.json"
        output.parent.mkdir()

        exit_code, stdout, stderr, optimizer = self.invoke(
            [first, second],
            output,
            orchestrator_version="cli-orchestrator-v2",
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "wrote orchestration result: completed\n")
        self.assertEqual(stderr, "")
        result = self.read_result(output)
        self.assertEqual(result.overall_status, "completed")
        self.assertEqual(result.model_version, "cli-model-v1")
        self.assertEqual(result.orchestrator_version, "cli-orchestrator-v2")
        self.assertEqual(optimizer.calls, ["station-2", "station-1"])
        self.assertEqual(
            {station.input_ref for station in result.stations},
            {"input-1", "input-2"},
        )
        rendered = output.read_text(encoding="utf-8")
        self.assertNotIn(str(self.temp_dir), rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_valid_and_invalid_inputs_write_partial_failure_and_return_one(self):
        valid = self.write_request("station-valid")
        invalid = self.temp_dir / "private-secret-name.json"
        invalid.write_text("{not-json", encoding="utf-8")
        output = self.temp_dir / "result.json"

        exit_code, stdout, stderr, optimizer = self.invoke(
            [valid, invalid], output
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "wrote orchestration result: partial_failure\n")
        self.assertEqual(stderr, "")
        result = self.read_result(output)
        self.assertEqual(result.overall_status, "partial_failure")
        self.assertEqual(optimizer.calls, ["station-valid"])
        self.assertEqual(
            {station.input_ref for station in result.stations},
            {"input-1", "input-2"},
        )
        rendered = output.read_text(encoding="utf-8")
        self.assertNotIn(str(invalid), rendered)
        self.assertNotIn("private-secret-name.json", rendered)
        self.assertNotIn("not-json", rendered)

    def test_all_invalid_inputs_write_failed_result_and_return_one(self):
        missing = self.temp_dir / "hidden-missing.json"
        malformed = self.temp_dir / "hidden-malformed.json"
        malformed.write_text("not-json", encoding="utf-8")
        output = self.temp_dir / "result.json"

        exit_code, stdout, stderr, optimizer = self.invoke(
            [missing, malformed], output
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "wrote orchestration result: failed\n")
        self.assertEqual(stderr, "")
        result = self.read_result(output)
        self.assertEqual(result.overall_status, "failed")
        self.assertEqual(
            result.orchestrator_version,
            "m4-orchestrator-b1-v1",
        )
        self.assertEqual(optimizer.calls, [])
        self.assertEqual(
            {station.input_ref for station in result.stations},
            {"input-1", "input-2"},
        )
        rendered = output.read_text(encoding="utf-8")
        self.assertNotIn(str(self.temp_dir), rendered)
        self.assertNotIn("hidden-missing.json", rendered)
        self.assertNotIn("hidden-malformed.json", rendered)

    def test_duplicate_station_ids_call_no_optimizer_and_return_one(self):
        first = self.write_request("station-duplicate", name="first.json")
        second = self.write_request("station-duplicate", name="second.json")
        output = self.temp_dir / "result.json"

        exit_code, stdout, stderr, optimizer = self.invoke(
            [first, second], output
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "wrote orchestration result: failed\n")
        self.assertEqual(stderr, "")
        self.assertEqual(optimizer.calls, [])
        result = self.read_result(output)
        self.assertEqual(result.overall_status, "failed")
        self.assertEqual(result.errors[0].code, "DUPLICATE_STATION_ID")

    def test_missing_output_directory_returns_one_with_safe_stderr_only(self):
        input_path = self.write_request("station-valid")
        output = self.temp_dir / "secret-output-directory" / "result.json"

        exit_code, stdout, stderr, optimizer = self.invoke([input_path], output)

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "failed to write orchestration result\n")
        self.assertEqual(optimizer.calls, ["station-valid"])
        self.assertFalse(output.exists())
        self.assertNotIn(str(output), stderr)

    def test_missing_required_arguments_raise_argparse_system_exit_two(self):
        cases = (
            [],
            ["--output", "result.json", "--model-version", "model-v1"],
            ["--input", "station.json", "--model-version", "model-v1"],
            ["--input", "station.json", "--output", "result.json"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                with redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_module_help_is_available_without_running_orchestration(self):
        completed = subprocess.run(
            [sys.executable, "-m", "m4_orchestrator", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertIn("--input", completed.stdout)
        self.assertIn("--output", completed.stdout)
        self.assertIn("--model-version", completed.stdout)
        self.assertIn("--orchestrator-version", completed.stdout)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
