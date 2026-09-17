"""Station-2 table-only commissioning writer; EMS execution must stay inhibited.

This is deliberately not an atomic live-device schedule switch. An uncertain
write or crash leaves a persistent station hold for operator reconciliation.
"""
from datetime import datetime
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from shared.project import get_project
from .control_sources import _NoRedirect
from .ems_model_update import ModelUpdateError, ZONE, _stamp


class EMSTableWriter:
    def __init__(self, adapter, root, token, *, enabled=False, opener=None):
        self.adapter, self.root, self.token = adapter, Path(root), token
        self.enabled = enabled
        self.opener = opener or build_opener(_NoRedirect())

    def status(self, station):
        enabled = self.enabled and station == 'station-2' and get_project().id == 'vifa'
        result = dict(station_id=station, enabled=enabled, status='waiting' if enabled else 'disabled',
            device_execution_status='unverified')
        try:
            journals = sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in journals:
                record = json.loads(path.read_text())
                if record.get('station_id') == station:
                    result.update({key: record[key] for key in ('status', 'run_id', 'effective_at',
                        'completed_operations', 'reason', 'network_write_performed') if key in record})
                    result['operation_counts'] = {key: len(value) for key, value in record.get('operations', {}).items()}
                    break
            if (self.root/(station+'.hold')).exists():
                result.update(status='table_write_unconfirmed', reason='计划表写入待核对，后续写入已暂停。')
        except (OSError, ValueError, TypeError):
            result.update(status='unavailable', reason='计划表写入记录暂不可用。')
        return result

    def submit(self, station, payload, configuration, *, now=None):
        if not self.enabled or station != 'station-2' or get_project().id != 'vifa':
            return dict(status='disabled', network_write_performed=False, device_execution_status='unverified')
        if not self.token or any(c.isspace() for c in self.token):
            raise ModelUpdateError('计划表写入凭据不可用。')
        prepared = self.adapter.preview(station, payload, configuration, now=now, fixed_cabinet_power=True)
        run_id = prepared['run_id']  # UUID validated by adapter
        self.root.mkdir(parents=True, exist_ok=True)
        journal = self.root / (run_id+'.json')
        hold = self.root / (station+'.hold')
        if journal.exists():
            return json.loads(journal.read_text())
        outcome = dict(status='table_write_unconfirmed', run_id=run_id,
            station_id=station, network_write_performed=False,
            device_execution_status='unverified', commissioning_only=True, completed_operations=0,
            effective_at=prepared['effective_at'], configuration_version=prepared['configuration_version'],
            operations=prepared['operations'], pending_operation=None, verified_operations=[])
        try:
            fd = os.open(hold, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return dict(outcome, status='blocked', reason='此前计划表写入待核对，已暂停后续写入。')
        with os.fdopen(fd, 'w') as output:
            output.write(run_id)
            output.flush()
            os.fsync(output.fileno())

        def save():
            temporary = journal.with_suffix('.tmp')
            with temporary.open('w') as output:
                json.dump(outcome, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(journal)

        try:
            save()  # durable intent before the first POST
            # Only for inhibited EMS commissioning. Live use requires atomic activation.
            for action in ('destroy', 'update', 'create'):
                for operation in prepared['operations'][action]:
                    if (now or datetime.now(ZONE)) >= _stamp(prepared['effective_at']):
                        raise ModelUpdateError('已错过计划表写入窗口。')
                    before = self.adapter.reader._read_table('t_model')
                    identifier = operation['query'].get('filterByTk')
                    if action != 'create':
                        selected = [row for row in before if row.get('id') == identifier]
                        predicate = json.loads(operation['query']['filter'])['$and']
                        if len(selected) != 1 or any(selected[0].get(key) != expected['$eq']
                                for clause in predicate for key, expected in clause.items()):
                            raise ModelUpdateError('计划表已被其他操作修改，暂停写入。')
                    url = get_project().sources['m4_base_url'].rstrip('/')+'/'+operation['path']
                    if operation['query']:
                        url += '?'+urlencode(operation['query'])
                    command = Request(url, method='POST',
                        data=json.dumps(operation.get('body', {}), ensure_ascii=False).encode(),
                        headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})
                    if (now or datetime.now(ZONE)) >= _stamp(prepared['effective_at']):
                        raise ModelUpdateError('读取期间已错过计划表写入窗口。')
                    outcome['pending_operation'] = dict(action=action, request=operation)
                    outcome['network_write_performed'] = True
                    save()
                    with self.opener.open(command, timeout=8) as response:
                        raw = response.read(2*1024*1024+1)
                        if response.status != 200 or len(raw) > 2*1024*1024:
                            raise ModelUpdateError('计划表响应未确认。')
                        result = json.loads(raw)
                        if not isinstance(result, dict) or result.get('errors'):
                            raise ModelUpdateError('计划表响应未确认。')
                    if action == 'create':
                        identifier = (result.get('data') or {}).get('id')
                        if type(identifier) is not int or identifier <= 0 or any(row.get('id') == identifier for row in before):
                            raise ModelUpdateError('新增计划记录未确认。')
                    after = self.adapter.reader._read_table('t_model')
                    matches = [row for row in after if row.get('id') == identifier]
                    if action == 'destroy':
                        verified = not matches
                    else:
                        verified = len(matches) == 1 and all(matches[0].get(k) == v for k, v in operation['body'].items())
                    if not verified:
                        raise ModelUpdateError('计划表回读未确认。')
                    outcome['verified_operations'].append(dict(action=action, record_id=identifier))
                    outcome['pending_operation'] = None
                    outcome['completed_operations'] += 1
                    save()
            final = self.adapter.preview(station, payload, configuration, now=now, fixed_cabinet_power=True)
            if any(final['operations'].values()):
                raise ModelUpdateError('计划表最终回读不一致。')
            outcome['status'] = 'plan_table_readback_verified'
            save()
            hold.unlink()
        except Exception:
            # Hold survives timeout/crash; no blind retries or credential-bearing errors.
            outcome['reason'] = '计划表写入未完全确认，暂停后续写入，需核对记录。'
            try:
                save()
            except OSError:
                pass  # persistent hold still prevents another write after journal failure
        return outcome
