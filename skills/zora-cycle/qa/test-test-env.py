#!/usr/bin/env python3
"""Committed configuration, sandbox control and child-only environment regressions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

TEMPLATE = """jobs:
  test:
    docker:
      - image: node:22
        environment:
          NODE_ENV: test
          MESSAGE_BUS_AWS_REGION: us-east-2
          MONGO_MESSAGE_BUS_CRYPT_CSFLE: "false"
          MONGO_TEST_URI: mongodb://localhost:27017
      - image: localstack/localstack:3.8.1
        environment:
          SERVICES: sqs,sns,s3
    steps:
      - checkout
"""
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('test_env', HERE / 'qa-test-env.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.work)], check=True)
        (self.work / '.circleci/templates').mkdir(parents=True)
        (self.work / '.circleci/templates/job-definitions.yml').write_text(TEMPLATE)
        self.parent = dict(os.environ, QA_SANDBOX='1', QA_WORK=str(self.work), TEST_OLD='original', TEST_KEEP='inherited')

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, variables=None, services=None):
        service = dict(name='example', packageName='@org/example', path='apps/example', testEnvVars=variables or {})
        config = dict(services=[service] if services is None else services)
        (self.work / '.circleci/services.json').write_text(json.dumps(config))
        subprocess.run(['git', '-C', str(self.work), 'add', '.circleci'], check=True)
        subprocess.run(['git', '-C', str(self.work), '-c', 'commit.gpgsign=false', '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)

    def test_overlay_preserves_parent_and_converts_primitives(self):
        self.commit({'TEST_OLD': 'replacement', 'BOOL': True, 'NO': False, 'PORT': 25000, 'RATIO': 1.5})
        for selector in ('example', '@org/example', 'apps/example'):
            work, child, _, digest, names = helper.child_environment(selector, self.parent)
            self.assertEqual(work, str(self.work.resolve()))
            self.assertEqual(child['TEST_OLD'], 'replacement')
            self.assertEqual(child['TEST_KEEP'], 'inherited')
            self.assertEqual((child['BOOL'], child['NO'], child['PORT'], child['RATIO']), ('true', 'false', '25000', '1.5'))
            self.assertEqual(len(digest), 64)
            self.assertEqual(names, sorted(names))
        self.assertEqual(self.parent['TEST_OLD'], 'original')

    def test_committed_config_ignores_working_tree_edits(self):
        self.commit({'VALUE': 'committed'})
        (self.work / '.circleci/services.json').write_text('invalid local edit')
        self.assertEqual(helper.child_environment('example', self.parent)[1]['VALUE'], 'committed')

    def test_protected_keys_refused(self):
        for key in ('QA_SANDBOX', 'QA_WORK', 'DOCKER_HOST', 'HOME', 'PATH', 'KUBECONFIG',
                    'XDG_CACHE_HOME', 'CODEX_HOME', 'PLAYWRIGHT_BROWSERS_PATH'):
            with self.subTest(key=key):
                self.commit({key: 'unsafe'})
                with self.assertRaisesRegex(ValueError, 'protected'):
                    helper.child_environment('example', self.parent)

    def test_nonprimitive_values_refused(self):
        for value in (None, ['value'], {'nested': True}, float('nan')):
            self.commit({'INVALID': value})
            with self.assertRaises(ValueError):
                helper.child_environment('example', self.parent)

    def test_unknown_and_ambiguous_target_refused(self):
        self.commit()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            helper.child_environment('missing', self.parent)
        item = dict(name='example', packageName='alias', path='apps/example', testEnvVars={})
        self.commit(services=[item, item])
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            helper.child_environment('example', self.parent)

    def test_missing_sandbox_and_nested_checkout_refused(self):
        self.commit()
        with self.assertRaisesRegex(ValueError, 'QA_SANDBOX'):
            helper.child_environment('example', self.parent | {'QA_SANDBOX': '0'})
        with self.assertRaisesRegex(ValueError, 'checkout root'):
            helper.child_environment('example', self.parent | {'QA_WORK': str(self.work / '.circleci')})

    def test_missing_configuration_refused(self):
        with self.assertRaises(ValueError):
            helper.child_environment('example', self.parent)

    def test_command_gets_literal_argv_and_logs_no_values(self):
        secret = 'unique-fixture-value-must-not-be-logged'
        self.commit({'TEST_OLD': secret})
        marker = self.work / 'unexpected-shell-command'
        literal = '$(touch ' + str(marker) + '); echo should-not-run'
        program = 'import os,sys; assert os.environ["TEST_OLD"]==sys.argv[1]; assert os.getcwd()==sys.argv[2]; assert sys.argv[3].startswith("$(touch ")'
        result = subprocess.run([sys.executable, str(HERE / 'qa-test-env.py'), 'example', sys.executable,
                                 '-c', program, secret, str(self.work.resolve()), literal],
                                env=self.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['variable_names'], ['MESSAGE_BUS_AWS_REGION', 'MONGO_MESSAGE_BUS_CRYPT_CSFLE', 'NODE_ENV', 'TEST_OLD'])
        self.assertEqual(self.parent['TEST_OLD'], 'original')

    def test_shared_defaults_are_committed_and_service_overrides_win(self):
        self.commit({'MESSAGE_BUS_AWS_REGION': 'service-region'})
        (self.work / '.circleci/templates/job-definitions.yml').write_text('invalid local edit')
        _, child, _, digest, _ = helper.child_environment('example', self.parent)
        self.assertEqual(child['MESSAGE_BUS_AWS_REGION'], 'service-region')
        self.assertEqual(child['MONGO_MESSAGE_BUS_CRYPT_CSFLE'], 'false')
        self.assertNotIn('SERVICES', child)
        self.assertNotIn('MONGO_TEST_URI', child)
        explicit = self.parent | {'MONGO_TEST_URI': 'mongodb://explicit-test-replica'}
        self.assertEqual(helper.child_environment('example', explicit)[1]['MONGO_TEST_URI'], explicit['MONGO_TEST_URI'])
        self.assertEqual(len(digest), 64)

    def test_shared_template_format_and_control_changes_fail_closed(self):
        for text in (TEMPLATE.replace('        environment:', '        env:', 1),
                     TEMPLATE.replace('          NODE_ENV: test', '          HOME: /bad'),
                     TEMPLATE.replace('          NODE_ENV: test', '          VALUES: [one,two]'),
                     TEMPLATE.replace('          NODE_ENV: test', '          NODE_ENV: test\n          NODE_ENV: other')):
            (self.work / '.circleci/templates/job-definitions.yml').write_text(text)
            self.commit()
            with self.assertRaises(ValueError):
                helper.child_environment('example', self.parent)

    def test_child_exit_code_preserved(self):
        self.commit()
        result = subprocess.run([sys.executable, str(HERE / 'qa-test-env.py'), 'example',
                                 sys.executable, '-c', 'raise SystemExit(17)'], env=self.parent, capture_output=True)
        self.assertEqual(result.returncode, 17)


if __name__ == '__main__':
    unittest.main()
