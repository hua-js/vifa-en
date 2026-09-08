"""One serialized Ollama request with raw evidence and uncertain-completion gate."""
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from m4_settings.control_sources import _NoRedirect
from m4_selection.live_chain import write

ROOT=Path(__file__).resolve().parents[1]
# Share the earlier probes' gate so a live trial cannot overlap an old model call.
GATE_DIR=ROOT/'m4/evaluations/2026-09-08-ollama-physical-probe'
MAX_BYTES=512*1024
MAX_LINE=64*1024


class OllamaSelector:
    def __init__(self,base_url,*,gate_dir=GATE_DIR,opener=None,model=None,
                 request_timeout_seconds=120.0,stream_budget_seconds=180.0):
        parts=urlsplit(base_url)
        if (parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password
                or parts.path not in ('','/') or parts.query or parts.fragment):
            raise ValueError('Ollama URL must be an explicit origin without credentials')
        self.endpoint=base_url.rstrip('/')+'/api/chat'
        self.model=model
        self.request_timeout_seconds=request_timeout_seconds
        self.stream_budget_seconds=stream_budget_seconds
        self.gate_dir=Path(gate_dir)
        self.opener=opener or build_opener(ProxyHandler({}),_NoRedirect())

    @classmethod
    def from_config(cls,config,**kwargs):
        if not config.enabled:raise ValueError('AI is disabled')
        return cls(config.base_url,model=config.model,request_timeout_seconds=config.request_timeout_seconds,
                   stream_budget_seconds=config.stream_budget_seconds,**kwargs)

    def __call__(self,payload,output):
        if (not isinstance(payload.get('model'),str) or not payload['model'].strip()
                or self.model is not None and payload['model']!=self.model
                or payload.get('stream') is not True or payload.get('keep_alive')!=0):
            raise ValueError('only the configured serial model preview payload is supported')
        output=Path(output);output.mkdir(parents=True,exist_ok=True)
        self.gate_dir.mkdir(parents=True,exist_ok=True)
        unknown=self.gate_dir/'server-completion-unknown.json'
        with (self.gate_dir/'serial.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('another Qwen request is running') from None
            if unknown.exists():raise ValueError('previous Qwen completion is unknown; do not resend')
            if (output/'ai-transport.json').exists():raise ValueError('AI output directory already used; do not resend')
            started=time.monotonic();answer=[];completed=False;final=None;size=0
            record=dict(model=payload['model'],endpoint=self.endpoint,completed=False,
                request_timeout_seconds=self.request_timeout_seconds,stream_budget_seconds=self.stream_budget_seconds,
                started_at=datetime.now(timezone.utc).isoformat())
            write(output/'ai-transport.json',record)
            write(output/'ai-request.json',payload)
            request=Request(self.endpoint,data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode(),
                headers={'Content-Type':'application/json'},method='POST')
            # Persist before any network I/O: process termination must not allow
            # a new client to overlap a remotely unfinished inference.
            write(unknown,dict(output=str(output.resolve()),model=payload['model'],status='in_flight',
                action='Confirm remote request completion before another model call.'))
            try:
                with self.opener.open(request,timeout=min(self.request_timeout_seconds,self.stream_budget_seconds)) as response, (output/'ai-response.ndjson').open('wb') as raw:
                    while True:
                        remaining=self.stream_budget_seconds-(time.monotonic()-started)
                        if remaining<=0:raise ValueError('Model stream time budget exceeded')
                        # urllib HTTPResponse exposes its socket through the
                        # buffered reader. Bound the next network read by the
                        # remaining run budget as well as the configured I/O limit.
                        sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
                        if sock is not None:sock.settimeout(min(self.request_timeout_seconds,remaining))
                        line=response.readline(MAX_LINE+1)
                        if not line:break
                        raw.write(line);raw.flush();size+=len(line)
                        if len(line)>MAX_LINE or size>MAX_BYTES:raise ValueError('Qwen stream exceeds size limit')
                        item=json.loads(line)
                        if item.get('error'):raise ValueError('Qwen returned an error')
                        chunk=item.get('message',{}).get('content','')
                        if not isinstance(chunk,str):raise ValueError('Qwen content is not text')
                        answer.append(chunk)
                        if item.get('done') is True:
                            completed=True;final=item
                        if time.monotonic()-started>=self.stream_budget_seconds:
                            raise ValueError('Model stream time budget exceeded')
                        if completed:break
                if not completed:raise ValueError('Qwen stream ended without completion')
                if final.get('done_reason')!='stop':raise ValueError('Qwen output did not finish normally')
                return ''.join(answer)
            except Exception as error:
                record['error']=type(error).__name__+': '+str(error)[:300]
                if not completed:
                    write(unknown,dict(output=str(output.resolve()),model=payload['model'],
                        action='Confirm remote request completion before another model call.'))
                raise
            finally:
                (output/'ai-answer.txt').write_text(''.join(answer))
                if final is not None:write(output/'ai-final-envelope.json',final)
                record.update(completed=completed,wall_seconds=time.monotonic()-started,
                    finished_at=datetime.now(timezone.utc).isoformat(),done_reason=final.get('done_reason') if final else None)
                write(output/'ai-transport.json',record)
                if completed:unknown.unlink()
