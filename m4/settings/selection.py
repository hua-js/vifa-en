"""Versioned local preferences and live checks around selection previews."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from m4.optimizer.contracts import NonNegativeFloat, OptimizationRequest, OptimizationResult, ProfileId, StrictModel
from m4.selection import SelectionPolicy, SelectionResult
from m4.selection.solver_selection import select_candidate_with_solver as select_candidate
from m4.selection.contracts import PreferenceMetric
from .candidates import CandidateError
from .live_inputs import request_from_inputs
from .objectives import get_profiles
from .store import SettingsConflict, SettingsStore


class PolicyPreferences(StrictModel):
    metric: PreferenceMetric
    demand_peak_tolerance_kw: NonNegativeFloat
    demand_energy_tolerance_kwh: NonNegativeFloat
    metric_tolerance: NonNegativeFloat
    tie_order: list[ProfileId]

    @model_validator(mode='after')
    def validate_order(self):
        if len(self.tie_order) != 3 or set(self.tie_order) != {'balanced', 'cost', 'pv'}:
            raise ValueError('并列顺序必须包含三个不同候选')
        if self.metric == 'profile_priority' and self.metric_tolerance != 0:
            raise ValueError('按候选顺序选择时，排名容差必须为 0')
        return self


class StationPolicy(StrictModel):
    station_id: str
    revision: int = Field(default=0, ge=0)
    version: str | None = None
    updated_at: datetime | None = None
    policy: SelectionPolicy | None = None

    @model_validator(mode='after')
    def validate_binding(self):
        if self.policy is not None and (self.policy.station_id != self.station_id or self.policy.version != self.version):
            raise ValueError('偏好与站点/版本不匹配')
        return self


class SavePolicy(StrictModel):
    expected_revision: int = Field(ge=0)
    preferences: PolicyPreferences | None


class SelectCandidate(StrictModel):
    candidate_run_id: str = Field(min_length=1, max_length=500)
    policy_revision: int = Field(ge=0)


class LiveSelectionResult(StrictModel):
    schema_version: Literal['m4-live-selection-v1'] = 'm4-live-selection-v1'
    station_id: str
    candidate_run_id: str
    configuration_version: str
    policy_revision: int
    checked_at: datetime
    expires_at: datetime
    selection: SelectionResult


class PolicyStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS m4_selection_policies (station_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            connection.commit()
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _get(connection, station_id):
        row = connection.execute('SELECT document FROM m4_selection_policies WHERE station_id=?', (station_id,)).fetchone()
        result = StationPolicy.model_validate_json(row[0]) if row else StationPolicy(station_id=station_id)
        if result.station_id != station_id:
            raise ValueError('偏好存储的站点不匹配')
        return result

    def get(self, station_id) -> StationPolicy:
        SettingsStore.validate_station(station_id)
        with self.connection() as connection:
            return self._get(connection, station_id)

    def save(self, station_id, preferences, *, expected_revision) -> StationPolicy:
        SettingsStore.validate_station(station_id)
        body = SavePolicy(expected_revision=expected_revision, preferences=preferences)
        if body.preferences is not None:
            body.preferences = PolicyPreferences.model_validate(body.preferences.model_dump())
        with self.connection() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = self._get(connection, station_id)
            if previous.revision != expected_revision:
                raise SettingsConflict('选择偏好已被其他窗口更新，请重新读取后保存')
            revision = previous.revision + 1
            payload = body.preferences.model_dump() if body.preferences is not None else None
            digest = sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
            version = f'{station_id}-selection-v{revision}-{digest}'
            policy = SelectionPolicy(station_id=station_id, policy_id=f'{station_id}-selection',
                                     version=version, **payload) if payload is not None else None
            result = StationPolicy(station_id=station_id, revision=revision, version=version,
                                   updated_at=datetime.now(timezone.utc), policy=policy)
            connection.execute('INSERT INTO m4_selection_policies (station_id, document) VALUES (?, ?) '
                'ON CONFLICT(station_id) DO UPDATE SET document=excluded.document', (station_id, result.model_dump_json()))
            return result


def _decision_content(request):
    data = request.model_dump()
    # Re-sampling an otherwise identical live state need not force a new solve.
    # Every new sample is independently validated; original expiry never extends.
    data.pop('request_id')
    data.pop('input_observed_at')
    data['source_versions'].pop('capability', None)
    return data


def _cabinet_content(bundle):
    """Only a sample timestamp may refresh without changing the cabinet state."""
    return sorted(
        [{key: value for key, value in cabinet.items() if key != 'observed_at'}
         for cabinet in bundle['sources']['realtime']['cabinets']],
        key=lambda cabinet: cabinet['emu_sn'],
    )


class LiveSelectionService:
    def __init__(self, *, store, policies, candidates, fetch_inputs, clock=None):
        self.store = store
        self.policies = policies
        self.candidates = candidates
        self.fetch_inputs = fetch_inputs
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _policy(self, station_id, revision):
        try:
            config = self.policies.get(station_id)
        except Exception:
            raise CandidateError(503, '无法复核选择偏好，请重新读取') from None
        if config.revision != revision:
            raise CandidateError(409, '选择偏好版本已变化，请重新读取后选择')
        return config

    def _verify_live(self, station_id, run_id):
        snapshot = self.candidates.latest(station_id)
        if not snapshot or snapshot['result']['run_id'] != run_id or snapshot.get('stale'):
            raise CandidateError(409, '候选已更新或过期，请重新计算后选择')
        try:
            configuration = self.store.get(station_id)
        except Exception:
            raise CandidateError(503, '无法复核调度参数，请重试') from None
        if configuration.version != snapshot['configuration_version']:
            raise CandidateError(409, '调度参数已更新，请重新计算')
        try:
            current = self.fetch_inputs(configuration)
        except Exception:
            raise CandidateError(502, '当前输入读取失败，未生成选择结果') from None
        if any(source.get('status') == 'error' for source in current.get('sources', {}).values()):
            raise CandidateError(502, '当前输入来源读取或校验失败，请重试')
        try:
            original = OptimizationRequest.model_validate_json(json.dumps(snapshot['request']))
            profiles = get_profiles(station_id)
            request_from_inputs(configuration, snapshot['inputs'], profiles, now=self.clock())
            fresh = request_from_inputs(configuration, current, profiles, now=self.clock())
            if _decision_content(fresh) != _decision_content(original):
                raise ValueError('decision input changed')
            if _cabinet_content(current) != _cabinet_content(snapshot['inputs']):
                raise ValueError('cabinet state changed')
            if self.clock() >= datetime.fromisoformat(snapshot['expires_at']):
                raise ValueError('expired')
        except (ValueError, TypeError, KeyError, OverflowError):
            raise CandidateError(409, '输入、参与柜或控制版本已变化或过期，请重新计算') from None
        return snapshot

    def select(self, station_id, candidate_run_id, policy_revision) -> LiveSelectionResult:
        SettingsStore.validate_station(station_id)
        policy = self._policy(station_id, policy_revision)
        snapshot = self._verify_live(station_id, candidate_run_id)
        try:
            request = OptimizationRequest.model_validate_json(json.dumps(snapshot['request']))
            output = OptimizationResult.model_validate_json(json.dumps(snapshot['result']['stations'][0]['optimization_result']))
            selection = select_candidate(request, output, policy.policy)
        except (ValueError, KeyError, TypeError, OverflowError):
            raise CandidateError(422, '候选独立复验失败，未生成选择结果') from None
        checked = self._verify_live(station_id, candidate_run_id)
        if checked != snapshot:
            raise CandidateError(409, '候选快照已变化，请重新选择')
        self._policy(station_id, policy_revision)
        # Fetching inputs can overlap a new calculation or a settings save.
        latest = self.candidates.latest(station_id)
        if latest != snapshot or self.clock() >= datetime.fromisoformat(snapshot['expires_at']):
            raise CandidateError(409, '候选已变化或过期，请重新计算')
        return LiveSelectionResult(station_id=station_id, candidate_run_id=candidate_run_id,
            configuration_version=snapshot['configuration_version'], policy_revision=policy_revision,
            checked_at=self.clock(), expires_at=datetime.fromisoformat(snapshot['expires_at']), selection=selection)
