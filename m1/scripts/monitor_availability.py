"""Local, GET-only M1 availability observer. Never starts an M1 business service."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
ZONE = ZoneInfo('Asia/Shanghai')
URL = 'https://aivifa.hlszh.com/dashboard_energy_api'
PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>M1 在线状态监控</title>
<style>body{font:15px system-ui;background:#f4f7fb;color:#172033;margin:28px}h1{font-size:25px}section{background:white;border:1px solid #ddd;border-radius:12px;padding:20px;margin:16px 0}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #eee;padding:9px}small{color:#6f7d93}.bad{color:#cf4055}.good{color:#16855a}pre{white-space:pre-wrap}input{padding:8px;width:65%}button{padding:8px}</style>
<h1>M1 在线状态监控</h1><small id="duration">只读采样 · 每 20 秒 · 无并发重复请求</small>
<section id="summary">正在连接监控进程…</section>
<section><h3>设备状态</h3><small>按本地 M1 页面口径计算；不代表物理设备连通性验收。</small><table><thead><tr><th>设备</th><th>页面判定</th><th>原因</th><th>数据年龄</th><th>采集时间</th><th>柜体时间</th><th>源质量</th></tr></thead><tbody id="rows"></tbody></table></section>
<section><h3>最近变化与请求</h3><pre id="events"></pre></section>
<section><details><summary>更新监控凭据</summary><p>仅在本机进程内存使用，不写日志。</p><input id="credential" type="password" placeholder="M1 页面地址（含 token）或只读 token"><button id="connect">连接</button><span id="feedback"></span></details></section>
<script>
const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){try{const r=await fetch('/state',{cache:'no-store'}),s=await r.json(),x=s.latest||{};
document.querySelector('#duration').textContent='只读采样 · 每 20 秒 · 截止 '+s.ends_at+' · 无并发重复请求';
document.querySelector('#summary').innerHTML='<b>'+esc(s.phase)+'</b><p>已采样 '+s.count+' 次 · 状态变化 '+s.transitions+' 次 · 请求失败 '+s.failures+' 次</p><p>最近响应：'+esc(x.at)+' · HTTP '+esc(x.http)+' · '+esc(x.seconds)+' 秒 · 接口 '+esc(x.status)+'</p><p>来源错误：'+esc((x.source_errors||[]).join('、')||'无')+' · 服务端时钟差：'+esc(x.clock_delta_seconds)+' 秒</p><small>证据：'+esc(s.log)+'</small>';
document.querySelector('#rows').innerHTML=(s.devices||[]).map(d=>'<tr><td>'+esc(d.name)+'</td><td class="'+(d.online?'good':'bad')+'">'+(d.online?'在线':'离线')+'</td><td>'+esc(d.reason)+'</td><td>'+esc(d.age_seconds)+' 秒</td><td>'+esc(d.timestamp)+'</td><td>'+esc(d.cabinet_timestamp)+'</td><td>'+esc(d.quality)+'</td></tr>').join('');
document.querySelector('#events').textContent=s.events.slice(-40).reverse().join('\n');}catch(e){document.querySelector('#summary').textContent='监控进程连接失败，请检查本地进程。';}setTimeout(refresh,2000)}
document.querySelector('#connect').onclick=async()=>{let value=document.querySelector('#credential').value.trim();try{value=new URL(value).searchParams.get('token')||value}catch{};const r=await fetch('/credential',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:value})});document.querySelector('#credential').value='';document.querySelector('#feedback').textContent=r.ok?'已连接，等待采样':'凭据格式无效';};refresh();</script></html>'''


def stamp():
    return datetime.now(ZONE).isoformat(timespec='seconds')


def age(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZONE)
        return time.time() - parsed.timestamp()
    except (ValueError, TypeError):
        return None


def devices(data):
    rt = {str(r.get('fk_en')): r for r in data.get('realtime', [])}
    nodes = {str(n.get('id')): n for n in data.get('nodes', [])}
    result = []
    valid_pv = sum(bool(r.get('online')) and r.get('data_quality') == 'good'
                   and isinstance(r.get('input_power_kw'), (float, int))
                   and isinstance(r.get('output_power_kw'), (float, int))
                   for r in data.get('pv_inverters', []))
    for key, n in nodes.items():
        r = rt.get(key, {})
        if n.get('is_aggregate') or n.get('name') == '光伏工控机':
            continue
        delta = age(r.get('timestamp'))
        quality = r.get('data_quality')
        online = bool(r) and quality not in ('missing', 'stale') and delta is not None and delta < 60
        reason = ('无实时记录' if not r else '源标记 ' + quality if quality in ('missing', 'stale')
                  else '时间无效' if delta is None else '采集超过60秒' if delta >= 60 else '采集有效')
        parent = nodes.get(str(n.get('parentId')), {})
        if n.get('node_type') == 'meter' and parent.get('node_type') == 'pv_grid_ac':
            online = valid_pv > 0
            reason = f'有效逆变器 {valid_pv}/9'
        result.append({'id': key, 'name': n.get('name') or n.get('sn') or key,
                       'online': online, 'reason': reason, 'age_seconds': round(delta, 3) if delta is not None else None,
                       'timestamp': r.get('timestamp'), 'cabinet_timestamp': r.get('cabinet_timestamp'),
                       'quality': quality, 'run_status': r.get('run_status'), 'power': r.get('power')})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--minutes', type=int, default=1440)
    args = parser.parse_args()
    if not 1 <= args.minutes <= 10080:
        parser.error('--minutes must be between 1 and 10080')
    directory = ROOT / 'outputs/m1/monitor' / datetime.now(ZONE).strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / 'samples.jsonl'
    state = {'phase': '准备采样', 'count': 0, 'transitions': 0, 'failures': 0,
             'latest': {}, 'devices': [], 'events': [], 'log': str(log),
             'ends_at': (datetime.now(ZONE) + timedelta(minutes=args.minutes)).isoformat(timespec='seconds')}
    lock = threading.RLock()
    token_path = ROOT / '.local/m1-monitor/token'
    credential = {'token': token_path.read_text().strip() if token_path.exists() else '',
                  'deadline': time.monotonic() + args.minutes * 60}
    wake = threading.Event()

    def event(message):
        state['events'] = (state['events'] + [stamp() + ' ' + message])[-120:]

    def poll():
        while True:
            with lock:
                token = credential['token']
                expired = time.monotonic() >= credential['deadline']
                if not token or expired:
                    state['phase'] = '监控已结束' if expired else '等待有效凭据'
            if not token or expired:
                wake.wait(2)
                wake.clear()
                continue
            started = time.monotonic()
            row = {'at': stamp(), 'http': None, 'status': 'error'}
            observed = None
            try:
                request = urllib.request.Request(URL, headers={'Authorization': 'Bearer ' + token,
                                                               'Accept': 'application/json', 'Cache-Control': 'no-cache'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    row['http'] = response.status
                    from email.utils import parsedate_to_datetime
                    server_date = response.headers.get('Date')
                    row['clock_delta_seconds'] = round(time.time() - parsedate_to_datetime(server_date).timestamp(), 1) if server_date else None
                    body = json.load(response)
                row['status'] = body.get('status')
                data = body.get('data')
                if row['status'] == 'ok' and isinstance(data, dict):
                    observed = devices(data)
                    row['source_errors'] = list((data.get('source_errors') or {}).keys())
                    row['devices'] = observed
                    row['pv_inverters'] = [{k: r.get(k) for k in ('sn', 'timestamp', 'online', 'data_quality', 'status_code')} for r in data.get('pv_inverters', [])]
                    row['counts'] = {k: len(data.get(k, [])) for k in ('sites', 'nodes', 'realtime')}
                else:
                    row['error'] = '业务响应失败，无法据此判定设备离线'
            except urllib.error.HTTPError as error:
                row.update(http=error.code, error='HTTPError')
            except Exception as error:
                row['error'] = type(error).__name__
            row['seconds'] = round(time.monotonic() - started, 2)
            row['received_at'] = datetime.now(ZONE).isoformat(timespec='milliseconds')
            with lock:
                state['count'] += 1
                state['latest'] = row
                if observed is not None:
                    previous = {d['id']: d for d in state['devices']}
                    for d in observed:
                        before = previous.get(d['id'])
                        if before and before['online'] != d['online']:
                            state['transitions'] += 1
                            event(d['name'] + (' 恢复在线' if d['online'] else ' 转为离线') + '：' + d['reason'])
                    state['devices'] = observed
                    state['phase'] = '正在监控'
                    event(f"HTTP {row['http']} · {row['seconds']}s · 离线 {sum(not d['online'] for d in observed)}/{len(observed)} · 来源错误 {','.join(row['source_errors']) or '无'}")
                else:
                    state['failures'] += 1
                    event(f"采样失败 HTTP {row['http']} · {row.get('error')}")
                    state['phase'] = '请求失败，等待下一次采样'
                    if row['http'] in (401, 403) or (row['http'] == 200 and row['status'] != 'ok'):
                        credential['token'] = ''
                with log.open('a') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                print(state['events'][-1], flush=True)
            wake.wait(max(0, 20 - (time.monotonic() - started)))
            wake.clear()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def reply(self, code, body, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/state':
                with lock:
                    body = json.dumps(state, ensure_ascii=False).encode()
                self.reply(200, body, 'application/json; charset=utf-8')
            elif self.path == '/':
                self.reply(200, PAGE.encode(), 'text/html; charset=utf-8')
            else:
                self.reply(404, b'', 'text/plain')

        def do_POST(self):
            import re
            origin = self.headers.get('Origin')
            if self.path != '/credential' or origin != f'http://127.0.0.1:{args.port}':
                self.reply(403, b'', 'text/plain')
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 8192:
                    raise ValueError()
                token = json.loads(self.rfile.read(length))['token']
                if not re.fullmatch(r'[A-Za-z0-9._~-]{1,4096}', token):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                self.reply(400, b'', 'text/plain')
                return
            with lock:
                credential.update(token=token, deadline=time.monotonic() + args.minutes * 60)
                state['ends_at'] = (datetime.now(ZONE) + timedelta(minutes=args.minutes)).isoformat(timespec='seconds')
            wake.set()
            self.reply(200, b'{}', 'application/json')

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    registry = ROOT / 'runtime/m1/availability-monitor.json'
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({'pid': os.getpid(), 'log': str(log),
                                   'ends_at': state['ends_at'], 'url': f'http://127.0.0.1:{args.port}/'}, indent=2))
    threading.Thread(target=poll, daemon=True).start()
    print(f'M1 monitor http://127.0.0.1:{args.port} | {log}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
