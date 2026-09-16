import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class HostNetworkTests(unittest.TestCase):
    def test_explicit_loopback_binding(self):
        path = Path(__file__).resolve().parents[1] / 'deploy/backend/entrypoint.py'
        spec = importlib.util.spec_from_file_location('host_entrypoint_test', path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, 'read_token', return_value='test-token'), \
             patch.object(module, 'prepare_data'), \
             patch.object(module, 'drop_privileges'), \
             patch.object(module, 'assert_data_access'), \
             patch.dict(module.os.environ, {'M4_BIND_HOST': '127.0.0.1'}), \
             patch.object(module.os, 'execv') as execute:
            module.main()
            argv = execute.call_args.args[1]
            self.assertEqual(argv[argv.index('--host') + 1], '127.0.0.1')
