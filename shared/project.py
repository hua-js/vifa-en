"""Validated, credential-free, process-scoped private deployment inventory.

Only homogeneous cabinet capacity/power allocation is supported by M4. This
inventory does not introduce heterogeneous cabinet dispatch or device control.
Configuration changes require a process restart; fingerprints invalidate plans.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
SOURCE_KEYS = {'m1_base_url', 'm1_energy_base_url', 'm1_growatt_url', 'm4_base_url'}
M1_KEYS = {'pv_efficiency_min_pct', 'ess_self_loss_max_pct', 'load_spike_min_kw', 'refresh_interval_seconds'}


def _fields(value, expected, label):
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f'{label}: missing or unknown fields')


def _identifier(value):
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError('Invalid project, station, source or device identifier')
    return value


def validate_source_url(value):
    """Validate a trusted configuration URL, never a caller-supplied proxy target."""
    if type(value) is not str or not value or any(c.isspace() or ord(c) < 32 for c in value) or '\\' in value:
        raise ValueError('Invalid source URL')
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None
                or '?' in value or '#' in value or '%' in parts.netloc
                or any(p in ('.', '..') for p in parts.path.split('/')) or '%' in parts.path):
            raise ValueError
        parts.port
    except ValueError:
        raise ValueError('Source URL must exclude credentials, query and fragment') from None
    return value


def source_origin(value):
    parts = urlsplit(validate_source_url(value))
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)


def _devices(value):
    if type(value) is not list or len(value) != len(set(map(str, value))):
        raise ValueError('Device roster must be a unique list')
    return tuple(_identifier(item) for item in value)


@dataclass(frozen=True)
class Station:
    id: str
    source_code: str
    name: str
    cabinet_sns: tuple[str, ...]
    has_pv: bool
    pv_meter_sn: str | None
    inverter_sns: tuple[str, ...]
    policy: str
    grid_import_limit_kw: float | None
    alarm_advisory_cabinets: tuple[str, ...]
    ems_baseline: dict | None = None


@dataclass(frozen=True)
class Project:
    id: str
    site_id: str
    timezone: str
    stations: tuple[Station, ...]
    sources: dict[str, str]
    m1: dict
    fingerprint: str

    def station(self, identifier):
        for station in self.stations:
            if station.id == identifier:
                return station
        raise ValueError('未知电站')

    def public_metadata(self):
        return {'schema_version': 1, 'project_id': self.id, 'configuration_version': self.fingerprint,
                'stations': [{'id': s.id, 'source_code': s.source_code,
                              'name': s.name, 'has_pv': s.has_pv, 'policy': s.policy,
                              'grid_import_limit_kw': s.grid_import_limit_kw} for s in self.stations]}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def load_project(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    _fields(data, {'schema_version', 'project_id', 'site_id', 'timezone', 'stations', 'sources', 'm1'}, 'project')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('Unsupported project schema')
    project_id, site_id = _identifier(data['project_id']), _identifier(data['site_id'])
    if data['timezone'] != 'Asia/Shanghai':
        raise ValueError('Only Asia/Shanghai is currently supported')
    if (type(data['sources']) is not dict or not SOURCE_KEYS <= set(data['sources'])
            or set(data['sources']) - SOURCE_KEYS - {'m3_gateway_base_url'}):
        raise ValueError('sources: missing or unknown fields')
    sources = {key: validate_source_url(value) for key, value in data['sources'].items()}
    _fields(data['m1'], M1_KEYS, 'm1')
    for key, value in data['m1'].items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid M1 threshold')
        if key.endswith('_pct') and value > 100:
            raise ValueError('Invalid M1 percentage')
    if type(data['m1']['refresh_interval_seconds']) is not int or data['m1']['refresh_interval_seconds'] <= 0:
        raise ValueError('Invalid refresh interval')
    if type(data['stations']) is not list or not data['stations']:
        raise ValueError('At least one station is required')
    stations, ids, codes = [], set(), set()
    # Growatt and EMS serial namespaces overlap in the original installation.
    # Enforce cross-station ownership within each source namespace, not globally.
    ems_devices, inverters = set(), set()
    for item in data['stations']:
        item = {'ems_baseline': None, **item}
        _fields(item, set(Station.__dataclass_fields__), 'station')
        if item['ems_baseline'] is not None and type(item['ems_baseline']) is not dict:
            raise ValueError('Invalid EMS baseline configuration')
        sid, code = _identifier(item['id']), _identifier(item['source_code'])
        if sid in ids or code in codes:
            raise ValueError('Duplicate station id or source_code')
        ids.add(sid)
        codes.add(code)
        if type(item['name']) is not str or not item['name'].strip() or len(item['name']) > 100:
            raise ValueError('Invalid station name')
        cabinets = _devices(item['cabinet_sns'])
        solar = _devices(item['inverter_sns'])
        advisory = _devices(item['alarm_advisory_cabinets'])
        meter = item['pv_meter_sn']
        if meter is not None:
            _identifier(meter)
        if type(item['has_pv']) is not bool or not cabinets:
            raise ValueError('Invalid station capability')
        if (item['has_pv'] and meter is None) or (not item['has_pv'] and (meter is not None or solar)):
            raise ValueError('PV capability and equipment mismatch')
        if item['policy'] not in ('cost_first', 'peak_reserve') or (item['policy'] == 'peak_reserve' and not item['has_pv']):
            raise ValueError('Unsupported policy/capability combination')
        limit = item['grid_import_limit_kw']
        if item['policy'] == 'cost_first' and limit is None:
            raise ValueError('cost_first requires an explicit physical import limit')
        if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0):
            raise ValueError('Invalid grid import limit')
        owned = set(cabinets) | ({meter} if meter else set())
        if meter in cabinets or ems_devices & owned or inverters & set(solar):
            raise ValueError('Device ownership conflicts')
        if not set(advisory) <= set(cabinets):
            raise ValueError('Advisory alarm devices must belong to this station')
        ems_devices.update(owned)
        inverters.update(solar)
        stations.append(Station(sid, code, item['name'], cabinets, item['has_pv'], meter,
                                solar, item['policy'], float(limit) if limit is not None else None, advisory,
                                item['ems_baseline']))
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return Project(project_id, site_id, data['timezone'], tuple(stations), sources, dict(data['m1']), fingerprint)


@lru_cache(maxsize=1)
def get_project():
    path = Path(os.environ.get('VIFA_PROJECT_CONFIG', str(ROOT / 'config/projects/vifa.json')))
    if not path.is_absolute():
        path = ROOT / path
    return load_project(path)
