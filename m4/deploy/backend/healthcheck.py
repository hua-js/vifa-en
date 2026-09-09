"""Check only local parameter storage; never read upstream or start a plan."""
import json
import sys
import socket
from http.client import HTTPConnection

from entrypoint import drop_privileges, SOCKET_PATH


def main():
    connection = None
    try:
        drop_privileges()
        connection = HTTPConnection('localhost', timeout=3)
        connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.sock.settimeout(3)
        connection.sock.connect(str(SOCKET_PATH))
        connection.request('GET', '/m4-api/stations/station-1/settings', headers={'Accept':'application/json'})
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            return 1
        payload = json.loads(body)
        return 0 if isinstance(payload, dict) and payload.get('station_id') == 'station-1' else 1
    except Exception:
        print('M4 local parameter health check failed.', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == '__main__':
    raise SystemExit(main())
