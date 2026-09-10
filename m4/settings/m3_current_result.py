"""Read the M3 current-task endpoint including its live actual-value overlay."""
import json
import os
import re
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, build_opener
from .control_sources import _NoRedirect
from uuid import UUID

from .pv_on_demand import SocketConnection


class M3CurrentResult:
    def __init__(self, gateway_token=None):
        self._gateway_token = gateway_token

    def _gateway_get(self, path):
        latest = re.fullmatch(r'/v1/stations/(ES01|ES02)/custom-forecast-runs/latest\?interval_seconds=900&forecast_days=1', path)
        result = re.fullmatch(r'/v1/custom-forecast-runs/([a-f0-9-]{36})/result', path)
        if latest:
            suffix = ('station_1' if latest[1] == 'ES01' else 'station_2') + '/latest?interval_seconds=900&forecast_days=1'
        elif result:
            suffix = str(UUID(result[1])) + '/result'
        else:
            raise ValueError('M3 gateway route unavailable')
        token = self._gateway_token
        if not isinstance(token, str) or not 16 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('M3 platform authentication unavailable')
        request = Request('https://opdash.lvkpower.com/energy-forecast-api/custom-runs/' + suffix,
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, method='GET')
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            body = response.read(4*1024*1024+1)
            if response.status != 200 or len(body) > 4*1024*1024:
                raise ValueError('M3 current result unavailable')
            envelope = json.loads(body)
        if envelope.get('status') != 'ok' or not isinstance(envelope.get('data'), dict):
            raise ValueError('M3 gateway response invalid')
        return envelope['data']

    def get(self, path):
        transport = os.environ.get('M4_M3_TRANSPORT', 'socket')
        if transport == 'platform_gateway':
            return self._gateway_get(path)
        if transport != 'socket':
            raise ValueError('M3 transport unavailable')
        token_file = Path(os.environ.get('M4_M3_ADMIN_TOKEN_FILE', '/run/secrets/m3_admin_token'))
        token = token_file.read_text().strip()
        if not 16 <= len(token) <= 4096 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError('M3 authentication unavailable')
        connection = SocketConnection(os.environ.get('M4_M3_SOCKET_PATH', '/run/vifa-m3/worker.sock'))
        connection.timeout = 15
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json'})
            response = connection.getresponse()
            data = response.read(4*1024*1024+1)
            if response.status != 200 or len(data) > 4*1024*1024:
                raise ValueError('M3 current result unavailable')
            return json.loads(data)
        finally:
            connection.close()

    def read(self, station):
        if station not in ('ES01','ES02'):
            raise ValueError('unknown station')
        run = self.get(f'/v1/stations/{station}/custom-forecast-runs/latest?interval_seconds=900&forecast_days=1')
        identifier = str(UUID(run['run_id']))
        result = self.get('/v1/custom-forecast-runs/'+quote(identifier, safe='')+'/result')
        current = result['run']
        if current['run_id'] != run['run_id'] or current['station_id'] != station:
            raise ValueError('M3 result identity mismatch')
        loads = [s for s in result['series'] if s.get('unique_id') == 'station_total_load']
        if len(loads) != 1 or loads[0].get('unit') != 'kW':
            raise ValueError('M3 load series mismatch')
        return {'run': current, 'points': [dict(p, unique_id='station_total_load', run_id=current['run_id']) for p in loads[0]['points']]}
