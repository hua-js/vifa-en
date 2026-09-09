"""Check only local parameter storage; never read upstream or start a plan."""
import json
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from entrypoint import drop_privileges


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def main():
    try:
        drop_privileges()
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request('http://127.0.0.1:8844/m4-api/stations/station-1/settings',
                          headers={'Accept': 'application/json'}, method='GET')
        with opener.open(request, timeout=3) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return 1
            payload = json.loads(body)
        return 0 if isinstance(payload, dict) and payload.get('station_id') == 'station-1' else 1
    except Exception:
        print('M4 local parameter health check failed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
