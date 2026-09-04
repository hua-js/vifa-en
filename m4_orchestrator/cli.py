import argparse
import sys
from pathlib import Path
from typing import Sequence

from .loader import load_station_input
from .service import M4Orchestrator
from .writer import OutputWriteError, write_result_atomic


DEFAULT_ORCHESTRATOR_VERSION = "m4-orchestrator-b1-v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run offline M4 optimization for station input files."
    )
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        type=Path,
        metavar="PATH",
        help="station OptimizationRequest JSON file (repeatable)",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        metavar="PATH",
        help="orchestration result JSON file",
    )
    parser.add_argument(
        "--model-version",
        required=True,
        metavar="VALUE",
        help="optimizer model version",
    )
    parser.add_argument(
        "--orchestrator-version",
        default=DEFAULT_ORCHESTRATOR_VERSION,
        metavar="VALUE",
        help="orchestrator version",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    station_inputs = [
        load_station_input(path, input_ref=f"input-{index}")
        for index, path in enumerate(args.input, start=1)
    ]
    result = M4Orchestrator(
        model_version=args.model_version,
        orchestrator_version=args.orchestrator_version,
    ).run(station_inputs)

    try:
        write_result_atomic(result, args.output)
    except OutputWriteError:
        print("failed to write orchestration result", file=sys.stderr)
        return 1

    print(f"wrote orchestration result: {result.overall_status}")
    return 0 if result.overall_status == "completed" else 1
