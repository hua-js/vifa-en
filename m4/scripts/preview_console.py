"""Preview the current HTML against an existing local M4 API, without restarting it."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


UPSTREAM = build_opener(NoRedirect)
PAGE = Path(__file__).resolve().parents[1] / 'web/M4优化调度控制台-线上版.html'
API_PATH = re.compile(r'/m4-api/stations/station-[12]/(?:settings|control-sources|daily-inputs|daily-plan|decision-history|decision-results/[a-f0-9-]{36})')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8847)
    parser.add_argument('--upstream-port', type=int, default=8848)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Query strings can contain platform access tokens.

        def respond(self, status, body, kind='application/json; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlsplit(self.path).path in ('/', '/m4'):
                return self.respond(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
            self.proxy()

        def do_POST(self):
            # Only an explicit same-origin UI calculation is accepted; no device route.
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (self.headers.get('Origin') != origin
                    or self.headers.get('Content-Type', '').split(';')[0] != 'application/json'
                    or not re.fullmatch(r'/m4-api/stations/station-[12]/daily-plan', self.path)):
                return self.respond(403, b'{"detail":"Preview request rejected"}')
            self.proxy()

        def proxy(self):
            if not API_PATH.fullmatch(urlsplit(self.path).path):
                return self.respond(404, b'{"detail":"Not found"}')
            headers = {'Content-Type': 'application/json'}
            if self.headers.get('Authorization'):
                headers['Authorization'] = self.headers['Authorization']
            request = Request(f'http://127.0.0.1:{args.upstream_port}{self.path}',
                              data=b'{}' if self.command == 'POST' else None,
                              headers=headers, method=self.command)
            try:
                with UPSTREAM.open(request, timeout=50) as response:
                    self.respond(response.status, response.read())
            except HTTPError as error:
                self.respond(error.code, error.read())
            except (URLError, TimeoutError):
                self.respond(502, b'{"detail":"Local M4 API unavailable"}')

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'PREVIEW=http://127.0.0.1:{server.server_port}/m4', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
