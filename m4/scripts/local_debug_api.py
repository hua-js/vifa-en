"""Loopback-only M4 preview using live read-only inputs and isolated local results."""
import argparse
import os
from pathlib import Path


def enable_mape_debug():
    """Override assessment only in this explicitly launched debug process."""
    from m4.settings import load_accuracy
    original = load_accuracy.assess

    def assess(evidence, station_id, now):
        result = original(evidence, station_id, now)
        # Only waive an evaluated threshold failure; malformed/stale evidence
        # and unavailable accuracy remain blocked.
        if result['status'] == 'blocked' and result['mape_percent'] is not None:
            result = {**result, 'status': 'ready', 'issues': [],
                      'debug_original_status': 'blocked',
                      'debug_original_issues': result['issues'],
                      'debug_mape_bypassed': True,
                      'version': 'local-debug-' + result['version']}
        return result

    load_accuracy.assess = assess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-mape', action='store_true', required=True)
    parser.add_argument('--port', type=int, default=8848)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    os.environ['M4_M3_TRANSPORT'] = 'platform_gateway'
    os.environ['M4_SETTINGS_DB'] = str(root / 'runtime/m4/local-debug/settings.sqlite3')
    os.environ['M4_DECISION_RESULTS_DIR'] = str(root / 'outputs/m4/local-debug/solver-decisions')
    enable_mape_debug()
    from m4.settings.api import create_app, upstream_token
    from m4.settings.models import StationConfiguration
    from m4.settings.store import SettingsStore
    from m4.scripts.preview_console import UPSTREAM
    from urllib.request import Request
    # Copy configuration through GET; never send changes to the online service.
    token = upstream_token()
    if not token:
        raise RuntimeError('Existing local upstream credential required')
    store = SettingsStore(Path(os.environ['M4_SETTINGS_DB']))
    for station in ('station-1', 'station-2'):
        request = Request('https://opdash.lvkpower.com/m4-api/stations/' + station + '/settings',
                          headers={'Authorization': 'Bearer ' + token}, method='GET')
        with UPSTREAM.open(request, timeout=40) as response:
            config = StationConfiguration.model_validate_json(response.read())
        if config.station_id != station:
            raise ValueError('Upstream station configuration mismatch')
        with store.connection() as connection:
            connection.execute('INSERT OR REPLACE INTO m4_station_settings VALUES (?, ?)',
                               (station, config.model_dump_json()))
            connection.commit()
    app = create_app()
    from fastapi.responses import JSONResponse

    @app.middleware('http')
    async def debug_routes(request, call_next):
        # Only local plan computation is writable; no configuration/device writes.
        import re
        if request.method != 'GET' and not (request.method == 'POST' and re.fullmatch(
                r'/m4-api/stations/station-[12]/daily-plan', request.url.path)):
            return JSONResponse({'detail': 'Local debug only permits plan calculation'}, status_code=403)
        response = await call_next(request)
        response.headers['X-M4-Local-Debug'] = 'mape-threshold-bypassed'
        return response

    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
