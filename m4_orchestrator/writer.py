import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .contracts import M4OrchestrationResult


class OutputWriteError(RuntimeError):
    """Raised when an orchestration result cannot be safely persisted."""


def serialize_result(result: M4OrchestrationResult) -> str:
    payload = result.model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def write_result_atomic(result: M4OrchestrationResult, path: Path) -> None:
    target = Path(path)
    temporary_path: Path | None = None
    write_failed = False

    try:
        temporary_file = NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary_path = Path(temporary_file.name)
        with temporary_file:
            temporary_file.write(serialize_result(result))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
    except BaseException as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not isinstance(error, Exception):
            raise
        write_failed = True

    if write_failed:
        raise OutputWriteError("orchestration result could not be written")
