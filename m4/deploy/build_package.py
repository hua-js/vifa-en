#!/usr/bin/env python3
"""Build a credential-free M4 release from explicit source allowlists."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = Path(__file__).resolve().parent
PACKAGES = ('m4_settings', 'm4_optimizer', 'm4_orchestrator', 'm4_selection')
HTML = ROOT / 'm4/M4优化调度控制台-线上版.html'
BACKEND_FILES = ('Dockerfile', 'compose.yaml', '.env.example', '.dockerignore',
                 'requirements.lock.txt', 'entrypoint.py', 'healthcheck.py', 'preflight.py')
NODE_RED_FILES = ('authorize.js', 'prepare_proxy.js', 'socket_request.js', 'finish_proxy.js',
                  'settings.fragment.js', 'env.example')


def flow(html):
    nodes = [
        dict(id='m4-customer-page', type='tab', label='M4 客户调度页面', disabled=False,
             info='iframe URL Token 由 Node-RED 校验；同源 /m4-api 转发至 Docker。环境变量见部署手册。'),
        dict(id='m4-page-in', type='http in', z='m4-customer-page', name='GET /m4',
             url='/m4', method='get', upload=False, swaggerDoc='', x=140, y=100,
             wires=[['m4-page-authorize']]),
        dict(id='m4-page-template', type='template', z='m4-customer-page',
             name='M4 HTML（纯文本）', field='payload', fieldType='msg', format='html',
             syntax='plain', template=html, output='str', x=370, y=100,
             wires=[['m4-page-response']]),
        dict(id='m4-page-response', type='http response', z='m4-customer-page',
             name='HTML 响应', statusCode='200',
             headers={'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'},
             x=610, y=100, wires=[]),
        dict(id='m4-api-response', type='http response', z='m4-customer-page',
             name='API / 错误响应', statusCode='', headers={}, x=990, y=320, wires=[]),
        dict(id='m4-proxy-catch', type='catch', z='m4-customer-page', name='连接错误',
             scope=['m4-backend-request'], uncaught=False, x=720, y=400, wires=[['m4-api-finish']]),
    ]
    def function(identifier, name, source, wires, x, y):
        nodes.append(dict(id=identifier, type='function', z='m4-customer-page', name=name,
                          func=(DEPLOY / 'node_red' / source).read_text(encoding='utf-8'),
                          outputs=len(wires), timeout=0, noerr=0, initialize='', finalize='',
                          libs=[], x=x, y=y, wires=wires))
    function('m4-page-authorize', '校验页面 Token', 'authorize.js',
             [['m4-page-template'], ['m4-api-response']], 260, 100)
    function('m4-api-authorize', '校验 API Token', 'authorize.js',
             [['m4-api-prepare'], ['m4-api-response']], 300, 260)
    function('m4-api-prepare', '限定接口与内部转发', 'prepare_proxy.js',
             [['m4-backend-request'], ['m4-api-response']], 520, 260)
    function('m4-api-finish', '整理 API 响应', 'finish_proxy.js',
             [['m4-api-response']], 910, 240)
    function('m4-backend-request', 'M4 Unix Socket 请求', 'socket_request.js',
             [['m4-api-finish']], 720, 240)
    routes = [('get', '/m4-api/stations/:stationId/:resource'),
              ('put', '/m4-api/stations/:stationId/:resource'),
              ('post', '/m4-api/stations/:stationId/:resource'),
              ('get', '/m4-api/stations/:stationId/decision-results/:runId')]
    for index, (method, url) in enumerate(routes):
        nodes.append(dict(id=f'm4-api-in-{index}', type='http in', z='m4-customer-page',
                          name=method.upper() + (' 历史详情' if index == 3 else ' M4 API'),
                          url=url, method=method, upload=False, swaggerDoc='', x=110,
                          y=220 + index * 60, wires=[['m4-api-authorize']]))
    return nodes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='New release directory; existing directories are not overwritten')
    args = parser.parse_args()
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=False)
    (target / 'node_red').mkdir()
    (target / 'secrets').mkdir()
    page = HTML.read_text(encoding='utf-8')
    (target / 'node_red/m4_customer_template.html').write_text(page, encoding='utf-8')
    (target / 'node_red/m4_customer_flow.json').write_text(
        json.dumps(flow(page), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (target / 'backend').mkdir()
    for filename in BACKEND_FILES:
        shutil.copyfile(DEPLOY / 'backend' / filename, target / 'backend' / filename)
    for filename in NODE_RED_FILES:
        shutil.copyfile(DEPLOY / 'node_red' / filename, target / 'node_red' / filename)
    for package in PACKAGES:
        for source in sorted((ROOT / package).rglob('*.py')):
            if '__pycache__' in source.parts:
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(ROOT / package):
                raise ValueError('Backend sources must be regular files inside the package')
            destination = target / 'backend/app' / source.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    fallback = target / 'backend/app/m4' / HTML.name
    fallback.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HTML, fallback)
    shutil.copyfile(DEPLOY / '部署手册.md', target / '部署手册.md')
    shutil.copyfile(DEPLOY / 'architecture.svg', target / 'architecture.svg')
    if (DEPLOY / '验收记录.md').exists():
        shutil.copyfile(DEPLOY / '验收记录.md', target / '验收记录.md')
    (target / 'secrets/README.txt').write_text(
        '此目录不附带真实凭据。请按部署手册在服务器创建 nocobase_token.txt。\n'
        '仅放 NocoBase 只读 API Token；不要放入 HTML、Flow、镜像或版本库。\n', encoding='utf-8')
    (target / '.gitignore').write_text('secrets/*\n!secrets/README.txt\nbackend/.env\n', encoding='utf-8')
    manifest = []
    for path in sorted(target.rglob('*')):
        if path.is_file():
            manifest.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.relative_to(target).as_posix())
    (target / 'SHA256SUMS').write_text('\n'.join(manifest) + '\n', encoding='utf-8')
    archive = target.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(target.rglob('*')):
            if path.is_file():
                output.write(path, Path(target.name) / path.relative_to(target))
    print(json.dumps({'directory': str(target), 'archive': str(archive),
                      'files': len(manifest) + 1, 'html_sha256': hashlib.sha256(HTML.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
