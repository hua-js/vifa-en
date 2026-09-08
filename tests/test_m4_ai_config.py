import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from m4_selection.ai_config import ModelConfig, load_model_config, resolve_model_config
from m4_selection.live_chain import make_ai_payload, main
from m4_selection.ollama import OllamaSelector
from test_m4_selection import fixture, policy
from m4_selection import select_candidate
from test_m4_ollama_selector import Opener


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.config=self.path/'model.json'
        self.data={'enabled':True,'provider':'ollama','base_url':'http://localhost:11434','model':'another-local-model:7b',
            'think':None,'request_timeout_seconds':25.0,'stream_budget_seconds':70.0,
            'generation':{'temperature':0.2,'seed':11,'num_ctx':8192,'num_predict':256}}
        self.config.write_text(json.dumps(self.data))

    def test_change_model_options_timeouts_and_audit_from_file_without_code_change(self):
        config=load_model_config(self.config)
        request,result=fixture();preview=select_candidate(request,result,policy(request))
        envelope={'request':request.model_dump(mode='json'),'result':{'stations':[{'optimization_result':result.model_dump(mode='json')}]}}
        payload=make_ai_payload(envelope,preview,config)
        self.assertEqual(payload['model'],'another-local-model:7b')
        self.assertEqual(payload['options'],self.data['generation'])
        self.assertNotIn('think',payload)
        self.assertTrue(payload['stream']);self.assertEqual(payload['keep_alive'],0)
        class Capturing(Opener):
            def open(self,request,timeout):
                self.timeout=timeout;self.request=request
                return super().open(request,timeout)
        opener=Capturing(b'{"message":{"content":"ok"},"done":true,"done_reason":"stop"}\n')
        model=OllamaSelector.from_config(config,gate_dir=self.path/'gate',opener=opener)
        self.assertEqual(model(payload,self.path/'run'),'ok')
        self.assertEqual(opener.timeout,25.0)
        self.assertEqual(opener.request.full_url,'http://localhost:11434/api/chat')
        self.assertEqual(json.loads((self.path/'run/ai-transport.json').read_text())['model'],'another-local-model:7b')

    def test_cli_overrides_and_explicit_disable(self):
        config=resolve_model_config(self.config,base_url='http://127.0.0.1:11435',model='replacement:latest')
        self.assertEqual(config.base_url,'http://127.0.0.1:11435');self.assertEqual(config.model,'replacement:latest')
        self.assertEqual(config.generation.num_ctx,8192)
        disabled=resolve_model_config(self.config,base_url='http://127.0.0.1:11435',disable=True)
        self.assertFalse(disabled.enabled)
        self.assertEqual(json.loads(self.config.read_text()),self.data)

    def test_invalid_config_missing_file_duplicates_and_nonfinite_do_not_fallback(self):
        for delta in [{'model':' '},{'base_url':'http://user:secret@host'},{'base_url':'http://host/api/chat'},
                      {'request_timeout_seconds':0},{'stream_budget_seconds':float('nan')},
                      {'generation':{'num_predict':True}}, {'modle':'typo'}, {'provider':'unknown'}, {'think':'unknown'}]:
            with self.subTest(delta=delta),self.assertRaises(ValueError):
                ModelConfig.model_validate({**self.data,**delta})
        self.config.write_text('{"enabled":true,"enabled":false}')
        with self.assertRaises(ValueError):load_model_config(self.config)
        with self.assertRaises(ValueError):load_model_config(self.path/'absent.json')

    def test_model_mismatch_is_rejected_before_network(self):
        config=load_model_config(self.config);opener=Opener(b'')
        model=OllamaSelector.from_config(config,gate_dir=self.path/'gate',opener=opener)
        with self.assertRaises(ValueError):model({'model':'unconfigured','stream':True,'keep_alive':0},self.path/'run')
        self.assertEqual(opener.calls,0)

    def test_cli_config_file_beats_environment_and_disable_skips_model_creation(self):
        with patch.dict('os.environ',{'M4_AI_CONFIG':str(self.path/'missing.json')}), \
                patch('m4_selection.live_chain.LocalApi') as api, \
                patch('m4_selection.ollama.OllamaSelector.from_config') as transport, \
                patch('m4_selection.live_chain.run_chain',return_value={'status':'completed','selected':None}) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--ai-config',str(self.config),'--model','next-model:1b','--no-ai']),0)
            transport.assert_not_called()
            effective=run.call_args.args[3]
            self.assertEqual(effective.model,'next-model:1b')
            self.assertFalse(effective.enabled)

    def test_cli_uses_environment_config_and_fails_before_api_on_bad_config(self):
        with patch.dict('os.environ',{'M4_AI_CONFIG':str(self.config)}), \
                patch('m4_selection.live_chain.LocalApi') as api, \
                patch('m4_selection.ollama.OllamaSelector.from_config') as transport, \
                patch('m4_selection.live_chain.run_chain',return_value={'status':'completed','selected':None}) as run, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main([]),0)
            self.assertEqual(run.call_args.args[3].model,self.data['model'])
            transport.assert_called_once()
        with patch('m4_selection.live_chain.LocalApi') as api, redirect_stdout(io.StringIO()):
            from contextlib import redirect_stderr
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as error:
                main(['--ai-config',str(self.path/'missing.json')])
            self.assertEqual(error.exception.code,2)
            api.assert_not_called()
