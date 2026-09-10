from dataclasses import dataclass
from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Literal, cast

from pydantic import HttpUrl, SecretStr, TypeAdapter


@dataclass(frozen=True)
class StationBinding:
    station_id: str
    station_key: Literal["station_1", "station_2"]
    station_name: str


def _exact_station_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty exact string")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain whitespace or control characters")
    return value


def _station_name(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("station_name must be a non-empty exact display name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("station_name must not contain control characters")
    return value


def parse_station_bindings(value: str) -> tuple[StationBinding, StationBinding]:
    """Parse the exact ordered public bindings without reading worker credentials."""
    if type(value) is not str:
        raise ValueError("M3_STATIONS_JSON must be an exact JSON string")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("M3_STATIONS_JSON must contain valid JSON") from error
    if type(payload) is not list or len(payload) != 2:
        raise ValueError("M3_STATIONS_JSON must contain exactly two stations")
    expected_keys = ("station_1", "station_2")
    bindings: list[StationBinding] = []
    for index, item in enumerate(payload):
        if type(item) is not dict or set(item) != {"station_id", "station_key", "station_name"}:
            raise ValueError("each station binding must be an exact object")
        station_id = _exact_station_text(item["station_id"], "station_id")
        station_key = item["station_key"]
        if station_key != expected_keys[index]:
            raise ValueError("station bindings must be ordered station_1, station_2")
        bindings.append(StationBinding(
            station_id=station_id,
            station_key=cast(Literal["station_1", "station_2"], station_key),
            station_name=_station_name(item["station_name"]),
        ))
    if bindings[0].station_id == bindings[1].station_id:
        raise ValueError("station IDs must be unique")
    if not bindings[0].station_id.endswith("ES01"):
        raise ValueError("station_1 full station_id must end in ES01")
    if not bindings[1].station_id.endswith("ES02"):
        raise ValueError("station_2 full station_id must end in ES02")
    return (bindings[0], bindings[1])


def _read_raw_source_token(environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    direct = environment.get("M3_RAW_SOURCE_API_TOKEN", "")
    token_file = environment.get("M3_RAW_SOURCE_API_TOKEN_FILE", "")
    if bool(direct.strip()) == bool(token_file.strip()):
        raise ValueError("configure exactly one raw source API token setting")
    if direct.strip():
        return direct
    try:
        values = Path(token_file).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("raw source API token file cannot be read") from error
    non_empty = [line.strip() for line in values if line.strip()]
    if len(non_empty) != 1:
        raise ValueError("raw source API token file must contain one non-empty token")
    return non_empty[0]


@dataclass(frozen=True)
class Settings:
    stations: tuple[StationBinding, StationBinding]
    raw_source_url: HttpUrl
    raw_source_api_token: SecretStr
    source_base_url: HttpUrl
    source_api_token: SecretStr
    nocobase_base_url: HttpUrl
    nocobase_api_key: SecretStr
    admin_api_token: SecretStr
    acceptance_enabled: bool = True
    timezone: str = "Asia/Shanghai"

    @property
    def station_ids(self) -> tuple[str, str]:
        return tuple(binding.station_id for binding in self.stations)

    @classmethod
    def from_env(cls) -> "Settings":
        required = (
            "M3_STATIONS_JSON", "M3_RAW_SOURCE_URL", "M3_SOURCE_BASE_URL",
            "M3_SOURCE_API_TOKEN", "M3_NOCOBASE_BASE_URL", "M3_NOCOBASE_API_KEY",
            "M3_ADMIN_API_TOKEN", "M3_ACCEPTANCE_ENABLED",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(f"missing required M3 settings: {','.join(missing)}")
        timezone = os.environ.get("M3_TIMEZONE", "Asia/Shanghai")
        if timezone != "Asia/Shanghai":
            raise ValueError("M3_TIMEZONE must be Asia/Shanghai")
        acceptance_value = os.environ["M3_ACCEPTANCE_ENABLED"]
        if acceptance_value not in {"true", "false"}:
            raise ValueError("M3_ACCEPTANCE_ENABLED must be true or false")
        url_adapter = TypeAdapter(HttpUrl)
        return cls(
            stations=parse_station_bindings(os.environ["M3_STATIONS_JSON"]),
            raw_source_url=url_adapter.validate_python(os.environ["M3_RAW_SOURCE_URL"]),
            raw_source_api_token=SecretStr(_read_raw_source_token()),
            source_base_url=url_adapter.validate_python(os.environ["M3_SOURCE_BASE_URL"]),
            source_api_token=SecretStr(os.environ["M3_SOURCE_API_TOKEN"]),
            nocobase_base_url=url_adapter.validate_python(os.environ["M3_NOCOBASE_BASE_URL"]),
            nocobase_api_key=SecretStr(os.environ["M3_NOCOBASE_API_KEY"]),
            admin_api_token=SecretStr(os.environ["M3_ADMIN_API_TOKEN"]),
            acceptance_enabled=acceptance_value == "true",
            timezone=timezone,
        )


@dataclass(frozen=True)
class DashboardSettings:
    """Least-privilege settings for the one-shot, read-only dashboard command."""

    stations: tuple[StationBinding, StationBinding]
    raw_source_url: HttpUrl
    raw_source_api_token: SecretStr
    nocobase_base_url: HttpUrl
    nocobase_api_key: SecretStr
    timezone: str = "Asia/Shanghai"

    @property
    def station_ids(self) -> tuple[str, str]:
        return tuple(binding.station_id for binding in self.stations)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "DashboardSettings":
        environment = os.environ if environ is None else environ
        required = (
            "M3_STATIONS_JSON",
            "M3_RAW_SOURCE_URL",
            "M3_NOCOBASE_BASE_URL",
            "M3_DASHBOARD_NOCOBASE_API_KEY",
        )
        missing = [
            name for name in required if not environment.get(name, "").strip()
        ]
        if missing:
            raise ValueError(
                f"missing required M3 dashboard settings: {','.join(missing)}"
            )
        timezone = environment.get("M3_TIMEZONE", "Asia/Shanghai")
        if timezone != "Asia/Shanghai":
            raise ValueError("M3_TIMEZONE must be Asia/Shanghai")
        url_adapter = TypeAdapter(HttpUrl)
        return cls(
            stations=parse_station_bindings(environment["M3_STATIONS_JSON"]),
            raw_source_url=url_adapter.validate_python(
                environment["M3_RAW_SOURCE_URL"]
            ),
            raw_source_api_token=SecretStr(_read_raw_source_token(environment)),
            nocobase_base_url=url_adapter.validate_python(
                environment["M3_NOCOBASE_BASE_URL"]
            ),
            nocobase_api_key=SecretStr(
                environment["M3_DASHBOARD_NOCOBASE_API_KEY"]
            ),
            timezone=timezone,
        )
