import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from m4.tests.test_m4_selection import fixture, policy


class SelectionCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.request, self.result = fixture()
        for name, model in [('request', self.request), ('result', self.result),
                            ('policy', policy(self.request, tie_order=['pv','cost','balanced']))]:
            (self.folder/f'{name}.json').write_text(model.model_dump_json())

    def run_cli(self, with_policy=True):
        args = [sys.executable, '-m', 'm4.selection', '--request', str(self.folder/'request.json'),
                '--result', str(self.folder/'result.json')]
        if with_policy: args += ['--policy', str(self.folder/'policy.json')]
        return subprocess.run(args, capture_output=True, text=True,
                              cwd=Path(__file__).resolve().parents[2], timeout=15)

    def test_selected_json_and_no_input_changes(self):
        before = {p.name:p.read_bytes() for p in self.folder.iterdir()}
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        answer = json.loads(proc.stdout)
        self.assertEqual(answer['selected']['profile_id'], 'pv')
        self.assertEqual(answer['dispatch_status'], 'not_dispatched')
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.folder.iterdir()})
        self.assertEqual(proc.stderr, '')

    def test_missing_policy_returns_pending_json_with_exit_one(self):
        proc = self.run_cli(False)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'pending_policy')

    def test_invalid_file_or_policy_outputs_only_error(self):
        for value in ['{', '{}', '{"metric_tolerance": NaN}']:
            with self.subTest(value=value):
                (self.folder/'policy.json').write_text(value)
                proc = self.run_cli()
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, '')
                self.assertTrue(proc.stderr)
        (self.folder/'policy.json').unlink()
        self.assertEqual(self.run_cli().returncode, 2)

    def test_duplicate_json_fields_are_rejected(self):
        path = self.folder/'policy.json'
        valid = path.read_text()
        path.write_text('{"metric":"preferred_soc_deviation",'+valid[1:])
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertIn('duplicate', proc.stderr)

    def test_unrepresentable_horizon_is_input_error(self):
        path = self.folder/'request.json'
        data = json.loads(path.read_text())
        data['plan_start_at'] = data['input_observed_at'] = '9999-12-31T23:45:00+08:00'
        path.write_text(json.dumps(data))
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, '')
        self.assertNotIn('Traceback', proc.stderr)
