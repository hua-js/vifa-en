import json
from pathlib import Path

from pydantic import ValidationError

from m4_optimizer.contracts import OptimizationRequest

from .contracts import ErrorCode, OrchestrationError, StationInput


def load_station_input(path: Path, *, input_ref: str | None = None) -> StationInput:
    safe_ref = input_ref or path.name
    if not isinstance(safe_ref, str) or not safe_ref.strip():
        raise ValueError("input_ref must be non-blank")

    station_id_hint: str | None = None
    request_id_hint: str | None = None

    def failed(code: ErrorCode, message: str) -> StationInput:
        return StationInput(
            input_ref=safe_ref,
            station_id_hint=station_id_hint,
            request_id_hint=request_id_hint,
            error=OrchestrationError(code=code, message=message),
        )

    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        if isinstance(payload, dict):
            station_id_hint = _nonblank_string(payload.get("station_id"))
            request_id_hint = _nonblank_string(payload.get("request_id"))
        request = OptimizationRequest.model_validate_json(text)
    except FileNotFoundError:
        return failed("INPUT_NOT_FOUND", "input file was not found")
    except UnicodeDecodeError:
        return failed("INPUT_ENCODING_ERROR", "input file is not valid UTF-8")
    except (json.JSONDecodeError, RecursionError):
        return failed("INPUT_JSON_ERROR", "input file is not valid JSON")
    except ValidationError:
        return failed("INPUT_VALIDATION_ERROR", "input does not match OptimizationRequest")
    except OSError:
        return failed("INPUT_READ_ERROR", "input file could not be read")

    return StationInput(
        input_ref=safe_ref,
        station_id_hint=station_id_hint,
        request_id_hint=request_id_hint,
        request=request,
    )


def _nonblank_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
