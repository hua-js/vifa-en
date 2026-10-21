"""Persistent day-plan coordinator, with independent one-shot night fallback.

Only the existing opt-in station-2 table writer can mutate an external table.
No rolling optimizer is instantiated or called here.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
import os
from threading import Lock, Thread
from uuid import uuid4

from shared.project import get_project
from .daily_dispatch import SCHEMA, daily_payload, night_payload
from .ems_model_update import ModelUpdateError, ZONE
from .ems_table_writer import PlanRegenerationRequired
from .frozen_baseline import planning_controls
from .night_charging import build_payload, configured_window, read_window, validate_freshness

POLICY = 'daily-schedule-ems-setpoint-v1'
CONFIRMED = 'plan_table_readback_verified'


class DailyScheduleService:
    def __init__(self, daily, writer):
        self.daily, self.writer = daily, writer
        self.root = daily.root / 'daily-dispatch'
        self.active, self.lock = set(), Lock()

    @property
    def running(self):
        return set(self.active)

    def _path(self, station, day=None):
        self.daily._path(station)
        day = day or datetime.now(ZONE).date().isoformat()
        return self.root / station / (day+'.json')

    def _read(self, station):
        path = self._path(station)
        if not path.exists():
            return dict(station_id=station, date=datetime.now(ZONE).date().isoformat(),
                policy_version=POLICY, status='waiting', message='等待当天日计划。')
        if path.is_symlink():
            raise ValueError('invalid day dispatch state')
        state = json.loads(path.read_text())
        if (state.get('station_id') != station or state.get('date') != path.stem
                or state.get('policy_version') != POLICY):
            raise ValueError('invalid day dispatch state')
        return state

    def _save(self, state):
        path = self._path(state['station_id'], state['date'])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def latest(self, station):
        state = self._read(station)
        # Read-only status; the next coordinator tick performs reconciliation.
        if state.get('status') == 'writing' and station not in self.active:
            state.update(status='blocked', message='上次日计划写入中断，等待自动核对。')
        return state

    def start(self, station, *, replace_run_id=None):
        self.daily._path(station)
        with self.lock:
            if station in self.running:
                return dict(station_id=station, status='running', message='计划任务正在处理中。')
            self.active.add(station)
        Thread(target=self._run, args=(station, replace_run_id), daemon=True).start()
        return dict(station_id=station, status='running', message='正在核对日计划下发。')

    def _submit(self, state, payload, configuration, *, snapshot=None):
        if self.daily.store.get(state['station_id']).version != configuration.version:
            raise ModelUpdateError('参数已变化，暂停计划写入。')
        if payload['kind'] != 'expire':
            controls = planning_controls(self.daily.inputs.live._controls(state['station_id'], validate_schedule=False))
            if controls['version'] != payload['request']['source_versions'].get('controls'):
                raise ModelUpdateError('EMS控制参数已变化，请重新生成日计划。')
            if payload['kind'] == 'night_fallback':
                validate_freshness(payload, configuration, datetime.now(ZONE))
        state.update(status='writing', pending_run_id=payload['run_id'],
            pending_effective_at=payload['effective_at'], pending_kind=payload['kind'],
            pending_daily_run_id=payload.get('daily_run_id'), pending_payload=deepcopy(payload),
            pending_snapshot=deepcopy(snapshot), message='正在写入计划表。')
        self._save(state)
        result = self.writer.submit(state['station_id'], payload, configuration)
        state['last_write'] = result
        if result.get('status') != CONFIRMED:
            if payload['kind'] != 'expire' and result.get('network_write_performed') is not False:
                self._invalidate_previous(state)
            state.update(status='blocked', message=result.get('reason', '计划表写入未确认，已暂停自动更新。'))
            self._save(state)
            return False
        self._confirm(state, payload, result, snapshot=snapshot)
        return True

    def _invalidate_previous(self, state):
        if state.get('active_plan'):
            state['interrupted_previous'] = {key: state.pop(key) for key in
                ('active_plan', 'active_daily') if key in state}

    def _regenerate(self, state, run_id):
        """Replace an unusable recovery target automatically, never replay it."""
        state.update(automatic_replan=True, replan_from_run_id=run_id,
            status='waiting', message='正在重新生成有效计划，完成后自动下发。')
        for key in ('recovery_daily_run_id', 'recovery_daily_snapshot', 'pending_run_id',
                    'pending_effective_at', 'pending_kind', 'pending_daily_run_id',
                    'pending_payload', 'pending_snapshot'):
            state.pop(key, None)
        self._save(state)
        if state['station_id'] not in self.daily.running:
            self.daily.start(state['station_id'])

    def _confirm(self, state, payload, result, *, snapshot=None):
        state.pop('pending_run_id', None)
        state.pop('pending_effective_at', None)
        state.pop('pending_kind', None)
        state.pop('pending_daily_run_id', None)
        state.pop('pending_payload', None)
        state.pop('pending_snapshot', None)
        state['last_write'] = result
        if payload['kind'] == 'expire':
            state['expired_rows_checked'] = True
        else:
            state['active_plan'] = dict(kind=payload['kind'], run_id=payload['run_id'],
                source=payload.get('source'),
                daily_run_id=payload.get('daily_run_id'), effective_at=payload['effective_at'],
                end_at=payload['end_at'], plan=deepcopy(payload['plan']),
                dispatch_plan=deepcopy(result['confirmed_plan']),
                schedule=result.get('confirmed_schedule', []),
                status=CONFIRMED, device_execution_status='unverified')
            if snapshot is not None:
                state['active_daily'] = deepcopy(snapshot)
                state['confirmed_daily_run_id'] = snapshot['run_id']
                state.pop('recovery_daily_run_id', None)
                state.pop('recovery_daily_snapshot', None)
                state.pop('automatic_replan', None)
                state.pop('replan_from_run_id', None)
            state.setdefault('versions', []).append(deepcopy(state['active_plan']))
        state.update(status='completed', message='日计划表已确认。' if snapshot else '保底安排已确认。')
        self._save(state)

    def _run(self, station, replace_run_id):
        state = None
        try:
            path = self._path(station)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix('.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                state = self._read(station)
                configuration = self.daily.store.get(station)
                write_status = self.writer.status(station)
                enabled = write_status.get('enabled') is True
                if state['status'] in ('writing', 'blocked') or write_status.get('status') == 'table_write_unconfirmed':
                    if not enabled:
                        return
                    recovery = self.writer.reconcile(station,
                        run_id=state.get('pending_run_id'))
                    if recovery.get('status') != 'ready':
                        state.update(status='blocked', message=recovery.get('reason', '计划表待核对，稍后自动重试。'))
                        self._save(state)
                        return
                    confirmed = recovery.get('confirmed_write')
                    if confirmed and state.get('pending_run_id') == confirmed['run_id']:
                        pending = state.get('pending_payload')
                        snapshot = state.get('pending_snapshot')
                        if (not pending or pending.get('run_id') != confirmed['run_id']
                                or pending.get('effective_at') != confirmed['effective_at']
                                or pending.get('date') != state['date']
                                or pending.get('kind') == 'daily' and not snapshot):
                            # The table has been fully reconciled. An old build
                            # without snapshots can regenerate future advice.
                            self._invalidate_previous(state)
                            self._regenerate(state, confirmed['run_id'])
                            return
                        # Restore the exact successful dispatch, never recut it
                        # with today's latest candidate or a later start window.
                        self._confirm(state, pending, confirmed, snapshot=snapshot)
                        return
                    pending_daily = (state.get('pending_daily_run_id') or recovery.get('daily_run_id')
                        or ((state.get('last_write') or {}).get('daily_run_id')))
                    if (pending_daily and state.get('confirmed_daily_run_id')
                            and pending_daily != state['confirmed_daily_run_id']):
                        # Preserve an explicitly requested replacement across
                        # retries; never announce the partially replaced old plan.
                        state['recovery_daily_run_id'] = pending_daily
                        if state.get('pending_snapshot'):
                            state['recovery_daily_snapshot'] = state['pending_snapshot']
                        self._invalidate_previous(state)
                    state.update(status='waiting', message='计划表已核对，等待新的有效计划。',
                        expired_rows_checked=False)
                    state.pop('pending_run_id', None)
                    state.pop('pending_effective_at', None)
                    state.pop('pending_kind', None)
                    state.pop('pending_daily_run_id', None)
                    state.pop('pending_payload', None)
                    state.pop('pending_snapshot', None)
                    self._save(state)
                if replace_run_id is None:
                    replace_run_id = state.get('recovery_daily_run_id')
                if enabled and not state.get('expired_rows_checked'):
                    now = datetime.now(ZONE)
                    expiry = dict(schema_version=SCHEMA, kind='expire', station_id=station,
                        date=now.date().isoformat(), run_id=str(uuid4()), plan=[],
                        effective_at=(now+timedelta(minutes=15)).isoformat())
                    if not self._submit(state, expiry, configuration):
                        return
                day = datetime.now(ZONE).date().isoformat()
                if day != state['date']:
                    raise ModelUpdateError('日期已变化，等待次日日计划。')
                if (state.get('confirmed_daily_run_id') and replace_run_id is None
                        and not state.get('automatic_replan')):
                    state.update(status='completed', message='已保留确认日计划；重算不会自动替换。')
                    self._save(state)
                    return
                recovering = replace_run_id == state.get('recovery_daily_run_id')
                daily = (state.get('recovery_daily_snapshot') if recovering else None) or self.daily.latest(station)
                comparison = (daily.get('result') or {}).get('record', {}).get('daily_comparison', {})
                ready = (daily.get('status') == 'completed' and comparison.get('date') == day
                    and comparison.get('recommended') is not None
                    and not daily.get('request', {}).get('source_versions', {}).get('startup_policy')
                    and daily['request']['source_versions'].get('configuration') == configuration.version)
                if ready:
                    controls = planning_controls(self.daily.inputs.live._controls(station, validate_schedule=False))
                    ready = controls['version'] == daily['request']['source_versions'].get('controls')
                if state.get('automatic_replan') and daily.get('run_id') == state.get('replan_from_run_id'):
                    self._regenerate(state, state.get('replan_from_run_id'))
                    return
                if recovering and replace_run_id is not None and (not ready or daily.get('run_id') != replace_run_id):
                    self._regenerate(state, replace_run_id)
                    return
                if replace_run_id is not None and (not ready or daily.get('run_id') != replace_run_id):
                    raise ModelUpdateError('待替换日计划已变化，请重新读取。')
                if state.get('confirmed_daily_run_id') and not state.get('automatic_replan'):
                    if replace_run_id is None or replace_run_id == state['confirmed_daily_run_id']:
                        state.update(status='completed', message='已保留确认日计划；重算不会自动替换。')
                        self._save(state)
                        return
                if ready:
                    if not enabled:
                        state.update(status='completed', message='日计划已生成，本站未启用计划表写入。')
                        self._save(state)
                        return
                    try:
                        payload = daily_payload(daily, configuration)
                    except ModelUpdateError:
                        if recovering and replace_run_id is not None:
                            self._regenerate(state, replace_run_id)
                            return
                        raise
                    self._submit(state, payload, configuration, snapshot=daily)
                    return
                # Night fallback is independent of the daily solver and runs only
                # until a complete day plan is admitted, once per valley window.
                now = datetime.now(ZONE)
                night = state.get('active_plan')
                if (enabled and not state.get('confirmed_daily_run_id') and not night
                        and configured_window(station, now)):
                    tariff = read_window(self.daily.inputs.live, station, now)
                    if tariff is not None:
                        payload = night_payload(build_payload(self.daily.inputs.live, configuration,
                            str(uuid4()), tariff=tariff))
                        if not self._submit(state, payload, configuration):
                            return
                if daily.get('status') != 'running':
                    self.daily.start(station)
                state.update(status='waiting', message='等待完整日计划，保底安排以计划表确认为准。')
                self._save(state)
        except PlanRegenerationRequired:
            if state is not None:
                self._regenerate(state, state.get('pending_daily_run_id') or state.get('pending_run_id'))
        except Exception as error:
            if state is not None:
                # Failed writes need read-only reconciliation on a later tick;
                # a fresh validation failure may retry after inputs recover.
                blocked = state.get('status') == 'writing'
                state.update(status='blocked' if blocked else 'failed',
                    message=str(error) if isinstance(error, ValueError) else '日计划下发暂不可用。')
                self._save(state)
        finally:
            with self.lock:
                self.active.discard(station)
