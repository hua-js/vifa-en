"""Read-only classification of an interrupted table write; never replays it."""
from datetime import datetime, timedelta
import json

from .ems_model_update import ModelUpdateError
from .ems_remaining_plan import matches_body, _clock


def ordered_operations(record):
    operations = record['operations']
    cutovers = set(record.get('cutover_record_ids', []))
    updates = operations['update']
    return ([('destroy', op) for op in operations['destroy']]
        + [('update', op) for op in updates if op['query']['filterByTk'] not in cutovers]
        + [('create', op) for op in operations['create']]
        + [('update', op) for op in updates if op['query']['filterByTk'] in cutovers])


def reconcile_record(record, rows, source, now):
    """Accept only uniquely observed effects or an unchanged pending target.

    Expired records may prove an old write occurred, but are never reactivated.
    Historical journals lack before-images: ambiguous partial updates stay held.
    """
    def conflict():
        raise ModelUpdateError('计划表与上次写入记录不一致，暂缓写入并自动复核。')

    indexed = {}
    windows = []
    for row in rows:
        owners = row.get('es_sn')
        if not isinstance(owners, list):
            conflict()
        if source not in owners:
            continue
        identifier = row.get('id')
        if owners != [source] or type(identifier) is not int or identifier <= 0 or identifier in indexed:
            conflict()
        indexed[identifier] = row
        if row.get('repeat') == '已过期':
            continue
        if row.get('repeat') not in ('今日有效', '每天重复'):
            conflict()
        if row.get('type') not in ('charge', 'discharge', 'pv_surplus_export'):
            conflict()
        day = row.get('m4_plan_date')
        if day:
            parsed = datetime.fromisoformat(day.replace('Z', '+00:00'))
            if parsed.time() != datetime.min.time():
                conflict()
            # The coordinator expires historical rows before any fresh dispatch.
            if parsed.date() < now.date():
                continue
            if parsed.date() > now.date():
                conflict()
        start = _clock(row.get('start_time'), now.date())
        end = _clock(row.get('end_time'), now.date(), end=True)
        if end < start and row.get('end_time') == '00:00:00':
            end += timedelta(days=1)
        if end <= start:
            conflict()
        if end > now:
            lane = 'export' if row['type'] == 'pv_surplus_export' else 'storage'
            if any(category == lane and begin < end and finish > start
                   for begin, finish, category in windows):
                raise ModelUpdateError('现有计划时段重叠，暂缓写入并自动复核。')
            windows.append((start, end, lane))

    def matches(row, body):
        if row is None:
            return False
        # Expiry is irreversible here. Only the repeat marker may differ.
        expected = dict(body)
        if row.get('repeat') == '已过期' and 'repeat' in expected:
            expected['repeat'] = '已过期'
        return matches_body(row, expected)

    def unchanged(row, operation):
        if row is None:
            return False
        clauses = json.loads(operation['query']['filter'])['$and']
        return all(row.get(key) == condition['$eq']
                   for clause in clauses for key, condition in clause.items())

    def classify(action, operation, identifier=None, before=None):
        if action == 'create':
            body = operation['body']
            if body.get('es_sn') != [source] or body.get('m4_run_id') != record['run_id']:
                conflict()
            # Include near matches so a changed power/repeat cannot look absent.
            candidates = [row for row in indexed.values()
                if row.get('start_time') == body['start_time']
                and row.get('end_time') == body['end_time']
                and row.get('type') == body['type']
                and (row.get('m4_run_id') == body['m4_run_id']
                     or row.get('repeat') != '已过期')]
            if not candidates:
                if identifier is not None:
                    conflict()
                return dict(action=action, result='not_applied')
            if (len(candidates) != 1 or not matches(candidates[0], body)
                    or identifier is not None and candidates[0]['id'] != identifier):
                conflict()
            row = candidates[0]
        else:
            target = operation['query']['filterByTk']
            if identifier is not None and identifier != target:
                conflict()
            row = indexed.get(target)
            # A moved/shared target must not be mistaken for a deleted target.
            if row is None and any(r.get('id') == target for r in rows):
                conflict()
            if action == 'destroy':
                if row is None:
                    return dict(action=action, record_id=target, result='applied')
                if identifier is None and unchanged(row, operation):
                    return dict(action=action, record_id=target, result='not_applied')
                conflict()
            elif action == 'update':
                body = operation['body']
                if before:
                    body = {k: v for k, v in before.items() if k != 'updatedAt'} | body
                elif 'm4_run_id' not in body:
                    # Legacy partial updates need ownership from the saved filter.
                    ownership = [c['m4_run_id']['$eq'] for c in
                        json.loads(operation['query']['filter'])['$and'] if 'm4_run_id' in c]
                    if not ownership or row is None or row.get('m4_run_id') != ownership[0]:
                        conflict()
                if not matches(row, body):
                    if identifier is None and unchanged(row, operation):
                        return dict(action=action, record_id=target, result='not_applied')
                    conflict()
            else:
                conflict()
        return dict(action=action, record_id=row['id'],
                    result='expired' if row.get('repeat') == '已过期' else 'applied')

    ordered = ordered_operations(record)
    verified = record['verified_operations']
    completed = record['completed_operations']
    if type(completed) is not int or completed != len(verified) or not 0 <= completed <= len(ordered):
        conflict()
    results = []
    for (action, operation), proof in zip(ordered, verified):
        if proof['action'] != action:
            conflict()
        results.append(classify(action, operation, proof['record_id'], proof.get('before')))
    pending = record.get('pending_operation')
    if pending:
        if completed >= len(ordered) or (pending['action'], pending['request']) != ordered[completed]:
            conflict()
        results.append(classify(pending['action'], pending['request'], before=pending.get('before')))
    if record.get('status') == 'plan_table_readback_verified':
        # A successful final readback can include unchanged rows which are not
        # present in the mutation journal. Check those before restoring a crash.
        for segment in record.get('confirmed_schedule', []):
            body = {key: segment[key] for key in ('es_sn', 'start_time', 'end_time',
                'type', 'kw', 'repeat', 'm4_run_id', 'm4_plan_date', 'explain') if key in segment}
            matches_now = [row for row in indexed.values() if matches_body(row, body)]
            if len(matches_now) != 1:
                conflict()
    return results
