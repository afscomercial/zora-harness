#!/usr/bin/env python3
"""Contract regressions for reconnect routing, bundle identity, and verdict binding."""
import argparse
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tarfile
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dispatch', HERE / 'qa-dispatch.py')
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


class DispatcherTests(unittest.TestCase):
    def test_dry_dispatch_bundles_sandbox_without_reading_pantheon(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name in dispatch.BUNDLE:
                (folder / name).write_text('fixture ' + name)
            (folder / 'qa-charter.md').write_text('frozen charter')
            args = argparse.Namespace(run=tmp, base='a'*40, commit='b'*40, profile='',
                                      services='user', repo='https://example.invalid/repo', worker='test', dry='1')
            with patch.object(dispatch, 'HERE', folder), \
                    patch.object(dispatch, 'worker_selection', return_value={'id': 'test', 'host': 'host', 'home': '/srv/qa'}), \
                    patch.object(dispatch.subprocess, 'run') as process:
                dispatch.submit(args)
                process.assert_not_called()
            record = json.loads(next((folder / 'qa').glob('*/attempts/*/dispatch.json')).read_text())
            self.assertEqual(record['protocol_version'], 5)
            self.assertEqual(record['execution_isolation'], 'sandbox')
            self.assertIsNone(record['profile'])
            self.assertEqual(record['services'], ['user'])
            self.assertNotIn('staging_isolation_supported', record)
            self.assertIn('qa-sandbox.sh', dispatch.BUNDLE)

    def test_bundle_hash_is_order_independent_and_content_bound(self):
        first = {'a': b'one', 'b': b'two'}
        self.assertEqual(dispatch.canonical_hash(first), dispatch.canonical_hash(dict(reversed(list(first.items())))))
        self.assertNotEqual(dispatch.canonical_hash(first), dispatch.canonical_hash({'a': b'two', 'b': b'one'}))

    def test_inventory_routes_explicit_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / 'workers.json'
            inventory.write_text(json.dumps({'default_worker': 'one', 'workers': [
                {'id': 'one', 'host': 'host-one', 'home': '/srv/qa'},
                {'id': 'two', 'host': 'host-two', 'home': '/srv/qa'}]}))
            with patch.dict(os.environ, {'ZORA_QA_WORKERS_FILE': str(inventory)}):
                self.assertEqual(dispatch.worker_selection('two')['host'], 'host-two')
                with self.assertRaises(ValueError):
                    dispatch.worker_selection('missing')

    def test_reconnect_uses_recorded_worker_and_never_submits(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = dict(protocol_version=5, execution_isolation='sandbox', job_id='job', worker_id='worker', environment_id='attempt', host='recorded-host', remote_home='/srv/qa',
                     remote_dir='/srv/qa/jobs/attempt', attempt_id='attempt')
            (Path(tmp) / 'dispatch.json').write_text(json.dumps(d))
            with patch.object(dispatch, 'ssh', return_value=b'queued\n') as call:
                with patch.dict(os.environ, {'ZORA_QA_HOST': 'different-host'}):
                    dispatch.reconnect('--status', tmp)
                self.assertEqual(call.call_args.args[0]['host'], 'recorded-host')
                self.assertIn('status attempt', call.call_args.args[1])
                self.assertNotIn('submit', call.call_args.args[1])

    def test_reconnect_rejects_legacy_or_malformed_identity_before_ssh(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(dispatch, 'ssh') as ssh:
            good = dict(protocol_version=5, execution_isolation='sandbox', job_id='job', worker_id='worker',
                        attempt_id='attempt', environment_id='attempt', host='host', remote_home='/srv/qa',
                        remote_dir='/srv/qa/jobs/attempt')
            for key, value in [('protocol_version', 4), ('execution_isolation', 'none'),
                               ('attempt_id', '../neighbor'), ('host', []), ('remote_home', []),
                               ('remote_dir', '/srv/qa/jobs/neighbor'), ('environment_id', 'neighbor')]:
                with self.subTest(key=key):
                    (Path(tmp) / 'dispatch.json').write_text(json.dumps(good | {key: value}))
                    with self.assertRaises(ValueError):
                        dispatch.reconnect('--status', tmp)
            ssh.assert_not_called()

    def test_concurrent_collectors_wait_for_complete_publication(self):
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode='w:gz') as archive:
            data = b'complete fixture evidence'
            member = tarfile.TarInfo('evidence/result.txt')
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        first_downloading = threading.Event()
        second_locking = threading.Event()
        release_first = threading.Event()
        second_downloading = threading.Event()
        errors, observed_prior_collection = [], []
        real_flock = dispatch.fcntl.flock
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def flock(handle, operation):
                if threading.current_thread().name == 'second' and operation == dispatch.fcntl.LOCK_EX:
                    second_locking.set()
                return real_flock(handle, operation)
            def download(d, command, output):
                if threading.current_thread().name == 'first':
                    first_downloading.set()
                    if not release_first.wait(5):
                        raise AssertionError('first collector was not released')
                else:
                    observed_prior_collection.append((directory / 'collection.json').exists())
                    second_downloading.set()
                output.write(bundle.getvalue())
            def collector():
                try:
                    dispatch.collect(directory, {'remote_dir': '/fixture'})
                except Exception as exc:
                    errors.append(exc)
            with patch.object(dispatch, 'status', return_value='done'), \
                    patch.object(dispatch, 'ssh', side_effect=download), \
                    patch.object(dispatch.fcntl, 'flock', side_effect=flock):
                first = threading.Thread(target=collector, name='first')
                second = threading.Thread(target=collector, name='second')
                first.start()
                try:
                    self.assertTrue(first_downloading.wait(5))
                    second.start()
                    self.assertTrue(second_locking.wait(5))
                    self.assertFalse(second_downloading.wait(.1))
                finally:
                    release_first.set()
                    first.join(5)
                    if second.ident is not None:
                        second.join(5)
                self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observed_prior_collection, [True])
            self.assertEqual((directory / 'evidence/result.txt').read_bytes(), data)
            self.assertEqual(json.loads((directory / 'collection.json').read_text())['status'], 'done')

    def test_collection_refuses_running_attempt_without_download(self):
        with patch.object(dispatch, 'status', return_value='validating'), patch.object(dispatch, 'ssh') as call:
            with self.assertRaises(ValueError):
                dispatch.collect(Path('/unused'), {})
            call.assert_not_called()


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in (['init', '-q'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'fixture']):
            subprocess.run(['git', '-c', 'commit.gpgsign=false', '-C', str(self.repo), *args], check=True)
        self.sha = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.run = self.root / 'attempt'
        (self.run / 'evidence').mkdir(parents=True)
        (self.run / 'evidence/gates.txt').write_text('observed output\n')
        self.d = dict(protocol_version=5, execution_isolation='sandbox', remote_home='/srv/qa', remote_dir='/srv/qa/jobs/attempt', job_id='logical', attempt_id='attempt', worker_id='one',
                      environment_id='attempt', commit=self.sha, base=self.sha, profile='infra', services=[],
                      repo='git@example.com:org/repo', charter_sha256='a'*64, bundle_sha256='b'*64)
        self.m = dict(self.d, runner_version=5, slot_id=1,
                      environment={'ready': True, 'sandbox_evidence': 'evidence/0-sandbox.json'}, codex={'ran': True, 'exit_code': 0},
                      checkout={'clean_before': True, 'head_before': self.sha, 'head_after': self.sha, 'tracked_changes_after': []})
        verdict = dict(verdict='PASS', commit=self.sha, findings=[], rungs=[
            dict(rung=i, status='pass' if i < 3 else 'skipped', reason='not applicable', evidence=['evidence/gates.txt'])
            for i in range(1, 6)])
        (self.run / 'verdict.json').write_text(json.dumps(verdict))
        self.sandbox = dict(attempt_id='attempt', unit='zora-qa-sandbox-attempt.service',
                            slice='zora-qa-sandbox-attempt.slice', cgroup='/fixture', init_pid=123,
                            docker_endpoint='unix:///srv/qa/jobs/attempt/sandbox/docker.sock', daemon_id='private-daemon',
                            namespaces={k: f'{prefix}:[200]' for k, prefix in [('mount', 'mnt'), ('net', 'net'), ('pid', 'pid'), ('uts', 'uts'), ('ipc', 'ipc')]},
                            host_namespaces={k: f'{prefix}:[100]' for k, prefix in [('mount', 'mnt'), ('net', 'net'), ('pid', 'pid'), ('uts', 'uts'), ('ipc', 'ipc')]})
        (self.run / 'evidence/0-sandbox.json').write_text(json.dumps(self.sandbox))
        self.m['files'] = {name: hashlib.sha256((self.run / name).read_bytes()).hexdigest()
                           for name in ['verdict.json', 'evidence/gates.txt', 'evidence/0-sandbox.json']}

    def tearDown(self):
        self.tmp.cleanup()

    def check(self):
        (self.run / 'dispatch.json').write_text(json.dumps(self.d))
        (self.run / 'remote-manifest.json').write_text(json.dumps(self.m))
        return subprocess.run(['bash', str(HERE.parent / 'verdict-check.sh'), str(self.run), str(self.repo), '--remote'], capture_output=True, text=True)

    def test_matching_protocol_passes(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_malformed_evidence_is_named_not_iterated(self):
        """A string evidence value is a malformed verdict, not one failure per character."""
        verdict = json.loads((self.run / 'verdict.json').read_text())
        verdict['rungs'][0]['evidence'] = 'evidence/gates.txt'
        (self.run / 'verdict.json').write_text(json.dumps(verdict))
        self.m['files']['verdict.json'] = hashlib.sha256((self.run / 'verdict.json').read_bytes()).hexdigest()
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('evidence must be a list of paths', result.stdout)
        # the old bug walked the string: one bogus complaint per character
        self.assertNotIn('evidence file missing: e', result.stdout)
        self.assertLessEqual(result.stdout.count('rung 1'), 1, result.stdout)

    def test_wrong_attempt_worker_or_hash_rejected(self):
        for key in ['attempt_id', 'worker_id', 'charter_sha256', 'bundle_sha256', 'profile']:
            with self.subTest(key=key):
                original = self.m[key]
                self.m[key] = 'wrong'
                self.assertEqual(self.check().returncode, 1)
                self.m[key] = original

    def test_actual_runner_manifest_matches_both_selection_modes(self):
        source = (HERE / 'qa-job.sh').read_text()
        anchor = 'python3 - "$OUT" "$DIR/tracked-after.txt" "$IN/dispatch.json"'
        body = source.split(anchor, 1)[1].split("\n", 1)[1].split("\nPY\n", 1)[0]
        tracked = self.root / 'tracked-after.txt'
        tracked.write_text('')
        for profile, services in [('infra', []), (None, ['api-gateway', 'user'])]:
            with self.subTest(profile=profile, services=services):
                self.d.update(profile=profile, services=services)
                expected = self.root / 'dispatch.json'
                (self.run / 'dispatch.json').unlink(missing_ok=True)
                expected.write_text(json.dumps(self.d))
                env = os.environ | {
                    'M_RUNNER_VERSION': '5', 'M_JOB': 'attempt', 'M_REPO': self.d['repo'],
                    'M_BASE': self.sha, 'M_COMMIT': self.sha, 'M_PROFILE': profile or '',
                    'M_SERVICES': ' '.join(services), 'M_STARTED_AT': '2026-01-01T00:00:00Z',
                    'M_HEAD_BEFORE': self.sha, 'M_HEAD_AFTER': self.sha, 'M_CLEAN_BEFORE': 'true',
                    'M_ENV_READY': 'true', 'M_CLUSTER': 'fixture', 'M_SEEDED': 'fixture',
                    'M_CODEX_RAN': 'true', 'M_CODEX_EXIT': '0', 'M_MODEL': 'fixture', 'M_EFFORT': 'high',
                    'QA_MANAGED': '1', 'QA_SLOT_ID': '1', 'QA_NET_ISOLATION': 'sandbox',
                    'QA_EXECUTION_ISOLATION': 'sandbox',
                }
                subprocess.run(['python3', '-c', body, str(self.run), str(tracked), str(expected)], env=env, check=True)
                self.m = json.loads((self.run / 'remote-manifest.json').read_text())
                result = self.check()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_selectors_are_rejected(self):
        for profile, services in [('infra', None), ('', ['user']), (None, 'user'), ('infra', ['user'])]:
            with self.subTest(profile=profile, services=services):
                self.m.update(profile=profile, services=services)
                self.assertEqual(self.check().returncode, 1)

    def test_new_manifest_cannot_downgrade(self):
        self.m.pop('protocol_version')
        self.m['runner_version'] = 3
        self.assertEqual(self.check().returncode, 1)

    def test_legacy_protocol_is_rejected(self):
        self.d.pop('protocol_version')
        self.m.pop('protocol_version')
        self.m['runner_version'] = 3
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_sandbox_identity_and_private_daemon_are_required(self):
        for key, value in [('attempt_id', 'neighbor'), ('unit', 'zora-qa-sandbox-neighbor.service'),
                           ('slice', 'system.slice'), ('docker_endpoint', 'unix:///var/run/docker.sock'),
                           ('daemon_id', ''), ('init_pid', True), ('namespaces', []),
                           ('host_namespaces', {}), ('namespaces', self.sandbox['host_namespaces'])]:
            with self.subTest(key=key):
                original = self.sandbox[key]
                self.sandbox[key] = value
                path = self.run / 'evidence/0-sandbox.json'
                path.write_text(json.dumps(self.sandbox))
                self.m['files']['evidence/0-sandbox.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
                result = self.check()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.sandbox[key] = original

    def test_missing_or_unhashed_sandbox_evidence_rejected(self):
        del self.m['files']['evidence/0-sandbox.json']
        self.assertEqual(self.check().returncode, 1)

    def test_execution_isolation_mismatch_rejected(self):
        self.m['execution_isolation'] = 'none'
        self.assertEqual(self.check().returncode, 1)

    def test_malformed_manifest_section_rejected_without_traceback(self):
        self.m['environment'] = ['invalid']
        result = self.check()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)

    def test_manifest_path_escape_rejected(self):
        self.m['files']['../outside'] = 'a'*64
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('unsafe manifest path', result.stdout)


if __name__ == '__main__':
    unittest.main()
