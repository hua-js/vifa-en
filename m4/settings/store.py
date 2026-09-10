"""Local SQLite persistence with transactional, per-station revision checks."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import StationConfiguration, StationParameters
from .runtime_config import effective_configuration, runtime_parameters


class SettingsConflict(ValueError):
    pass


class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def validate_station(station_id: str):
        if station_id not in ('station-1', 'station-2'):
            raise ValueError('未知电站')

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('''CREATE TABLE IF NOT EXISTS m4_station_settings (
                station_id TEXT PRIMARY KEY, document TEXT NOT NULL)''')
            connection.commit()
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _get(connection, station_id):
        row = connection.execute('SELECT document FROM m4_station_settings WHERE station_id=?', (station_id,)).fetchone()
        if row is None:
            return StationConfiguration(station_id=station_id)
        # Read legacy documents without rewriting them or reviving their former
        # manual control values. Only the three explicitly retired fields migrate.
        document = json.loads(row[0])
        if isinstance(document.get('parameters'), dict):
            for name in ('demand_limit_kw', 'grid_export_enabled', 'grid_export_limit_kw'):
                document['parameters'].pop(name, None)
        config = StationConfiguration.model_validate_json(json.dumps(document))
        if config.station_id != station_id:
            raise ValueError('配置电站与存储标识不一致')
        return config

    def get(self, station_id: str) -> StationConfiguration:
        self.validate_station(station_id)
        with self.connection() as connection:
            return effective_configuration(self._get(connection, station_id))

    def save(self, station_id: str, parameters: StationParameters, *, expected_revision: int) -> StationConfiguration:
        self.validate_station(station_id)
        parameters = StationParameters.model_validate({
            **parameters.model_dump(), **runtime_parameters(station_id),
        })
        with self.connection() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = self._get(connection, station_id)
            if previous.revision != expected_revision:
                raise SettingsConflict('参数已被其他窗口更新，请重新读取后再保存')
            if previous.parameters == parameters:
                return effective_configuration(previous)
            revision = previous.revision + 1
            digest = hashlib.sha256(json.dumps(parameters.model_dump(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
            config = StationConfiguration(station_id=station_id, revision=revision,
                version=f'{station_id}-settings-v{revision}-{digest}',
                updated_at=datetime.now(timezone.utc), parameters=parameters)
            connection.execute('INSERT INTO m4_station_settings (station_id,document) VALUES (?,?) '
                'ON CONFLICT(station_id) DO UPDATE SET document=excluded.document',
                (station_id, config.model_dump_json()))
            return effective_configuration(config)
