"""Exact, non-coercing JSON runtime validation shared by domain and HTTP edges."""

import json
import math
from typing import Any


MAX_JSON_DEPTH = 32


class ExactJsonError(ValueError):
    """A value cannot cross an exact JSON boundary without coercion."""


def validate_exact_json_value(
    value: object, field_name: str, *, depth: int = 0
) -> Any:
    """Return an exact JSON value or raise without normalizing Python types."""

    if depth > MAX_JSON_DEPTH:
        raise ExactJsonError(
            f"{field_name} exceeds the JSON structural depth limit"
        )
    value_type = type(value)
    if value is None or value_type in {bool, str, int}:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ExactJsonError(f"{field_name} contains a non-finite number")
        return value
    if value_type is dict:
        checked: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ExactJsonError(
                    f"{field_name} contains a non-string object key"
                )
            checked[key] = validate_exact_json_value(
                item, field_name, depth=depth + 1
            )
        return checked
    if value_type is list:
        return [
            validate_exact_json_value(item, field_name, depth=depth + 1)
            for item in value
        ]
    raise ExactJsonError(f"{field_name} contains a non-JSON value")


def validate_exact_json_mapping(
    value: object, field_name: str, *, allow_empty: bool
) -> dict[str, Any]:
    """Validate an exact built-in JSON object and its complete value graph."""

    if type(value) is not dict or (not value and not allow_empty):
        raise ExactJsonError(f"{field_name} must be a non-empty object")
    try:
        checked = validate_exact_json_value(value, field_name)
        json.dumps(
            checked,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except RecursionError as error:
        raise ExactJsonError(f"{field_name} exceeds safe JSON recursion") from error
    except (TypeError, ValueError) as error:
        raise ExactJsonError(f"{field_name} is not valid JSON") from error
    return checked
