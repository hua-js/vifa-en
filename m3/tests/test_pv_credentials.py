import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from m3.tests.test_pv_forecast_tools import load


class CredentialTests(unittest.TestCase):
    def read(self,path):
        assert importlib.util.find_spec('m3.worker.pv_credentials'), 'shared credential reader missing'
        from m3.worker.pv_credentials import read_token_file
        return read_token_file(path)

    def test_private_and_existing_group_read_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'raw-source.token';path.write_text('test-shared-token-0123456789abcdef\n')
            for mode in (0o400,0o600,0o440,0o640):
                path.chmod(mode)
                self.assertEqual(self.read(path),'test-shared-token-0123456789abcdef')

    def test_world_access_group_write_symlink_and_bad_content_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'raw-source.token';path.write_text('test-shared-token-0123456789abcdef')
            for mode in (0o644,0o660,0o666,0o700):
                path.chmod(mode)
                with self.assertRaises(ValueError): self.read(path)
            path.chmod(0o640)
            link=Path(temp)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError): self.read(link)
            for value in ('short','first-token-0123456789\nsecond-token-0123456789','x'*5000,'token contains spaces'):
                path.write_text(value)
                with self.assertRaises(ValueError): self.read(path)

    def test_import_query_and_service_reuse_same_0640_file(self):
        from m3.worker.pv_query_app import build_reader
        from m3.worker.pv_service_app import service_token
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'raw-source.token';value='test-shared-token-0123456789abcdef'
            path.write_text(value+'\n');path.chmod(0o640)
            with patch.dict(os.environ,{'PV_NOCOBASE_IMPORT_API_KEY':'',
                'PV_NOCOBASE_IMPORT_API_KEY_FILE':str(path),'PV_NOCOBASE_QUERY_API_KEY_FILE':str(path),
                'PV_SERVICE_TOKEN_FILE':str(path)}):
                self.assertEqual(load('import-weather-history.py').credential(),value)
                self.assertEqual(service_token(),value)
                self.assertTrue(callable(build_reader()))


if __name__=='__main__':unittest.main()
