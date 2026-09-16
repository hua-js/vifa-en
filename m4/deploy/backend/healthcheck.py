"""Check only local parameter storage; never read upstream or start a plan."""
import json
import re
import sys
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from entrypoint import drop_privileges


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def read_local(opener, path):
    request = Request('http://127.0.0.1:8844' + path,
                      headers={'Accept': 'application/json'}, method='GET')
    # Two local reads must fit inside Docker's five-second healthcheck timeout.
    with opener.open(request, timeout=2) as response:
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ValueError('invalid local response')
        return json.loads(body)


def main():
    try:
        drop_privileges()
        opener = build_opener(ProxyHandler({}), NoRedirect())
        project = read_local(opener, '/m4-api/project')
        if not isinstance(project, dict) or project.get('schema_version') != 1:
            return 1
        stations = project.get('stations')
        if not isinstance(stations, list) or not stations or not isinstance(stations[0], dict):
            return 1
        station_id = stations[0].get('id')
        if not isinstance(station_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', station_id):
            return 1
        # All stations share this instance's project-bound settings database.
        payload = read_local(opener, '/m4-api/stations/' + quote(station_id, safe='') + '/settings')
        return 0 if isinstance(payload, dict) and payload.get('station_id') == station_id else 1
    except Exception:
        print('M4 local parameter health check failed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
