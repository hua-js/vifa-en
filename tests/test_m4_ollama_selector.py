import io
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from m4_selection.ollama import OllamaSelector


class Opener:
    def __init__(self,body):self.body=body;self.calls=0
    def open(self,request,timeout):self.calls+=1;return io.BytesIO(self.body)


class OllamaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.output=self.path/'run';self.output.mkdir()
        self.payload={'model':'qwen3.5:4b','stream':True,'keep_alive':0,'think':False,'messages':[]}

    def test_completed_stream_keeps_raw_answer_and_completion_record(self):
        opener=Opener(b'{"message":{"content":"hello"},"done":false}\n{"message":{"content":" world"},"done":true,"done_reason":"stop"}\n')
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener)
        self.assertEqual(model(self.payload,self.output),'hello world')
        record=json.loads((self.output/'ai-transport.json').read_text())
        self.assertTrue(record['completed']);self.assertEqual(opener.calls,1)
        self.assertEqual((self.output/'ai-answer.txt').read_text(),'hello world')
        self.assertFalse((self.path/'gate/server-completion-unknown.json').exists())

    def test_incomplete_stream_blocks_followup_without_resending(self):
        opener=Opener(b'{"message":{"content":"partial"},"done":false}\n')
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener)
        with self.assertRaises(ValueError):model(self.payload,self.output)
        self.assertTrue((self.path/'gate/server-completion-unknown.json').exists())
        with self.assertRaises(ValueError):model(self.payload,self.output)
        self.assertEqual(opener.calls,1)

    def test_truncated_completed_output_is_rejected_without_unknown_completion(self):
        opener=Opener(b'{"message":{"content":"cut"},"done":true,"done_reason":"length"}\n')
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener)
        with self.assertRaises(ValueError):model(self.payload,self.output)
        self.assertFalse((self.path/'gate/server-completion-unknown.json').exists())
        self.assertTrue(json.loads((self.output/'ai-transport.json').read_text())['completed'])

    def test_endpoint_rejects_credentials_path_and_query(self):
        for url in ['http://user:secret@host','http://host/api/chat','file:///tmp/ai','http://host?token=secret']:
            with self.subTest(url=url),self.assertRaises(ValueError):OllamaSelector(url,gate_dir=self.path/'gate')

    def test_interrupted_request_keeps_global_completion_gate(self):
        class Interrupted:
            calls=0
            def open(self,request,timeout):
                self.calls+=1
                raise KeyboardInterrupt
        opener=Interrupted()
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener)
        with self.assertRaises(KeyboardInterrupt):model(self.payload,self.output)
        self.assertTrue((self.path/'gate/server-completion-unknown.json').exists())
        with self.assertRaises(ValueError):model(self.payload,self.path/'another-run')
        self.assertEqual(opener.calls,1)

    def test_occupied_serial_lock_never_sends_model_request(self):
        gate=self.path/'gate';gate.mkdir()
        opener=Opener(b'')
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=gate,opener=opener)
        with (gate/'serial.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'running'):model(self.payload,self.output)
        self.assertEqual(opener.calls,0)

    def test_late_done_is_recorded_complete_but_not_accepted_after_budget(self):
        opener=Opener(b'{"message":{"content":"late"},"done":true,"done_reason":"stop"}\n')
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener,
            request_timeout_seconds=120.0,stream_budget_seconds=1.0)
        with patch('m4_selection.ollama.time.monotonic',side_effect=[0.0,0.0,2.0,2.0]), \
                self.assertRaisesRegex(ValueError,'budget'):
            model(self.payload,self.output)
        self.assertTrue(json.loads((self.output/'ai-transport.json').read_text())['completed'])
        self.assertFalse((self.path/'gate/server-completion-unknown.json').exists())

    def test_each_socket_read_uses_remaining_budget(self):
        timeouts=[]
        sock=SimpleNamespace(settimeout=timeouts.append)
        class Response(io.BytesIO):
            fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock))
        class Capturing:
            def open(self,request,timeout):
                self.timeout=timeout
                return Response(b'{"message":{"content":"ok"},"done":false}\n{"done":true,"done_reason":"stop"}\n')
        opener=Capturing()
        model=OllamaSelector('http://127.0.0.1:11434',gate_dir=self.path/'gate',opener=opener,
            request_timeout_seconds=120.0,stream_budget_seconds=10.0)
        with patch('m4_selection.ollama.time.monotonic',side_effect=[0.0,2.0,3.0,9.0,9.5,9.5]):
            self.assertEqual(model(self.payload,self.output),'ok')
        self.assertEqual(opener.timeout,10.0)
        self.assertEqual(timeouts,[8.0,1.0])
