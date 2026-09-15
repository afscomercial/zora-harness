#!/usr/bin/env python3
"""Fail-closed sandbox guard tests. No namespaces, Docker daemon, or units are created."""
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).with_name('qa-sandbox.sh')

@unittest.skipUnless(platform.system() == 'Linux', 'sandbox runtime targets Linux')
class SandboxGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.attempt = self.root / 'jobs' / 'attempt-a'
        self.attempt.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.touched = self.root / 'host-command-called'
        for tool in ('docker', 'systemctl', 'nsenter'):
            command = self.bin / tool
            command.write_text(f'#!/bin/bash\ntouch "{self.touched}"\nexit 99\n')
            command.chmod(0o700)
        self.env = os.environ | {'PATH': str(self.bin) + ':' + os.environ['PATH'],
                                 'QA_HOME': str(self.root)}
        self.endpoint = f'unix://{self.attempt}/sandbox/docker.sock'

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, action, path=None):
        result = subprocess.run(['bash', str(HELPER), action, str(path or self.attempt)],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertFalse(self.touched.exists(), 'guard invoked host infrastructure before validating identity')
        return result

    def identity(self, **overrides):
        data = dict(attempt_id='attempt-a', docker_endpoint=self.endpoint,
                    unit='zora-qa-sandbox-attempt-a.service') | overrides
        (self.attempt / 'sandbox.json').write_text(json.dumps(data))

    def test_relative_path_rejected(self):
        self.assertNotEqual(self.invoke('down', 'relative').returncode, 0)

    def test_invalid_attempt_name_rejected(self):
        self.assertNotEqual(self.invoke('down', self.root / 'jobs' / 'invalid.name').returncode, 0)

    def test_down_without_owned_resources_is_idempotent(self):
        self.assertEqual(self.invoke('down').returncode, 0)
        self.assertEqual(self.invoke('down').returncode, 0)

    def test_orphan_identity_cannot_authorize_cleanup(self):
        self.identity()
        result = self.invoke('down')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('without ownership', result.stderr)

    def test_mismatched_attempt_rejected_before_docker(self):
        self.identity(attempt_id='another-attempt')
        self.env['DOCKER_HOST'] = self.endpoint
        self.assertNotEqual(self.invoke('check').returncode, 0)

    def test_host_docker_endpoint_rejected(self):
        self.identity()
        self.env['DOCKER_HOST'] = 'unix:///var/run/docker.sock'
        result = self.invoke('check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('private DOCKER_HOST required', result.stderr)

    def test_wrong_unit_rejected_before_systemctl(self):
        self.identity(unit='unrelated.service')
        self.assertNotEqual(self.invoke('verify').returncode, 0)

if __name__ == '__main__':
    unittest.main()
