"""Exercise release orchestration with real Git snapshots and a fake Docker CLI."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


DEPLOY = Path(__file__).resolve().parents[2] / 'm4/deploy'


class PublishScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='m4-publish-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        shutil.copytree(DEPLOY, self.repo / 'm4/deploy',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (self.repo / 'm4/web').mkdir()
        (self.repo / 'm4/__init__.py').write_text('')
        shutil.copytree(DEPLOY.parents[1] / 'docs/m4/deploy', self.repo / 'docs/m4/deploy')
        (self.repo / 'docs/产品化配置与交付.md').write_text('# Release fixture')
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts/check_project.py').write_text('# offline checker fixture\n')
        (self.repo / 'm4/web/M4优化调度控制台-线上版.html').write_text('<!doctype html><title>M4</title>')
        (self.repo / 'shared').mkdir()
        (self.repo / 'shared/__init__.py').write_text('')
        (self.repo / 'shared/project.py').write_text('# release fixture\n')
        (self.repo / 'config/projects').mkdir(parents=True)
        for name in ('vifa.json', 'example.json'):
            (self.repo / 'config/projects' / name).write_text('{"schema_version":1}')
        for package in ('m4/settings', 'm4/optimizer', 'm4/orchestrator', 'm4/selection'):
            (self.repo / package).mkdir()
            (self.repo / package / '__init__.py').write_text('# snapshot fixture\n')
        (self.repo / '.gitignore').write_text('local-secret.txt\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release-test@example.invalid')
        self.git('add', '.')
        self.git('-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                 'commit', '-qm', 'Release fixture')
        self.sha = self.git('rev-parse', 'HEAD').strip()
        (self.repo / 'local-secret.txt').write_text('fixture-only-do-not-package')
        self.output = self.root / 'release-m4-0.1.0'
        self.log = self.root / 'docker-calls.jsonl'
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        docker = bin_dir / 'docker'
        docker.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['MOCK_DOCKER_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[:2] == ['buildx', 'build'] and os.environ.get('MOCK_BUILD_FAIL'):
    sys.exit(9)
if args[:2] == ['image', 'inspect']:
    print(os.environ.get('MOCK_ARCH', 'linux/amd64'))
if args[:1] == ['push'] and os.environ.get('MOCK_PUSH_FAIL'):
    sys.exit(10)
''')
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                        CRR_IMAGE='registry.example.invalid/team/vifa', VERSION='m4-0.1.0',
                        RELEASE_DIR=str(self.output), MOCK_DOCKER_LOG=str(self.log))

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True)

    def run_script(self, **overrides):
        return subprocess.run(['bash', str(self.repo / 'm4/deploy/build-and-push.sh')],
                              env=dict(self.env, **overrides), capture_output=True,
                              text=True, timeout=30)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_release_uses_committed_snapshot_and_matching_image_metadata(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        reference = f'registry.example.invalid/team/vifa:m4-{self.sha[:12]}-amd64'
        metadata = json.loads((self.output / 'release.json').read_text())
        self.assertEqual(metadata['image'], reference)
        self.assertEqual(metadata['revision'], self.sha)
        self.assertEqual(metadata['platform'], 'linux/amd64')
        self.assertEqual(metadata['api_contract'], 'm4-project-v1')
        self.assertIn('M4_IMAGE=' + reference, (self.output / 'backend/.env.example').read_text())
        calls = self.calls()
        build = next(call for call in calls if call[:2] == ['buildx', 'build'])
        self.assertIn('--load', build)
        self.assertEqual(build[build.index('--platform') + 1], 'linux/amd64')
        self.assertEqual(build[-1], str(self.output / 'backend'))
        pushes = [call for call in calls if call[0] == 'push']
        self.assertEqual(pushes, [['push', reference], ['push', 'registry.example.invalid/team/vifa:0.1.0-amd64']])
        self.assertLess(calls.index(next(c for c in calls if c[:2] == ['image', 'inspect'])), calls.index(pushes[0]))
        self.assertEqual(sum(c[:2] == ['buildx', 'imagetools'] for c in calls), 2)
        self.assertFalse((self.output / 'local-secret.txt').exists())
        for line in (self.output / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        with zipfile.ZipFile(str(self.output) + '.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read(self.output.name + '/release.json'),
                             (self.output / 'release.json').read_bytes())

    def test_dirty_source_stops_before_docker(self):
        (self.repo / 'm4/settings/__init__.py').write_text('# uncommitted change')
        result = self.run_script()
        self.assertEqual(result.returncode, 65)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.output.exists())

    def test_unsupported_platform_is_rejected(self):
        result = self.run_script(PLATFORM='linux/arm/v7')
        self.assertEqual(result.returncode, 64)
        self.assertEqual(self.calls(), [])

    def test_arm64_uses_distinct_tags_and_matching_package(self):
        result = self.run_script(PLATFORM='linux/arm64', MOCK_ARCH='linux/arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((self.output / 'release.json').read_text())
        self.assertEqual(metadata['platform'], 'linux/arm64')
        self.assertEqual(metadata['image'], f'registry.example.invalid/team/vifa:m4-{self.sha[:12]}-arm64')
        self.assertIn('M4_PLATFORM=linux/arm64', (self.output / 'backend/.env.example').read_text())
        pushes = [call for call in self.calls() if call[0] == 'push']
        self.assertEqual(pushes[-1], ['push', 'registry.example.invalid/team/vifa:0.1.0-arm64'])

    def test_wrong_architecture_never_pushes(self):
        result = self.run_script(MOCK_ARCH='linux/arm64')
        self.assertEqual(result.returncode, 70)
        self.assertFalse(any(c[0] in ('tag', 'push') for c in self.calls()))
        self.assertNotIn('Published:', result.stdout)

    def test_build_failure_never_pushes(self):
        result = self.run_script(MOCK_BUILD_FAIL='1')
        self.assertEqual(result.returncode, 9)
        self.assertFalse(any(c[0] in ('tag', 'push') for c in self.calls()))
        self.assertNotIn('Published:', result.stdout)

    def test_failed_revision_push_stops_release_tag_and_success_message(self):
        result = self.run_script(MOCK_PUSH_FAIL='1')
        self.assertEqual(result.returncode, 10)
        self.assertEqual(sum(c[0] == 'push' for c in self.calls()), 1)
        self.assertFalse(any(c[:2] == ['buildx', 'imagetools'] for c in self.calls()))
        self.assertNotIn('Published:', result.stdout)


if __name__ == '__main__':
    unittest.main()
