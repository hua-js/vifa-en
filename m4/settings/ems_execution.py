"""Attach version-scoped table acceptance, not physical telemetry evidence."""


def annotate_execution(station, run_id, payload, points):
    write = payload.get('ems_table_write') or {}
    bound = (write.get('station_id') == station and write.get('run_id') == run_id
             and write.get('effective_at') == payload.get('effective_at'))
    verified = (bound and write.get('status') == 'plan_table_readback_verified'
                and write.get('execution_basis') == 'ems_plan_table_readback_v1'
                and write.get('plan_date') == payload.get('date'))
    confirmed = {p['timestamp']: p for p in write.get('confirmed_plan', [])} if verified else {}
    for point in points:
        evidence = confirmed.get(point['timestamp'])
        if evidence and all(evidence.get(k) == point.get(k) for k in ('mode', 'target_power_kw')):
            point['ems_execution'] = 'confirmed'
        elif bound and write.get('status') in ('table_write_unconfirmed', 'blocked'):
            point['ems_execution'] = 'unconfirmed'
        else:
            point['ems_execution'] = 'not_confirmed'
