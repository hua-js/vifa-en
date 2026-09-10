"""Read the M3 current-task endpoint including its live actual-value overlay."""
import json
import os
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from .pv_on_demand import SocketConnection


class M3CurrentResult:
    def get(self, path):
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
