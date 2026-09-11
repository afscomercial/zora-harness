#!/usr/bin/env python3
"""Linux runner regressions; mocks external tools and never contacts a provider."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).with_name('qa-job.sh')

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.job = self.root / 'jobs' / 'regression'
        (self.job / 'in').mkdir(parents=True)
        (self.job / 'job.log').touch()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'])
        self.args = ['--job','regression','--dir',str(self.job),'--repo','unused',
                     '--base','abc','--commit','abc','--profile','infrastructure']
        self.tool('docker', 'exit 0')
        self.tool('codex', 'if [ "$1" = --version ]; then echo mock; exit 0; fi\nexit 1')
        self.functions = self.root/'functions.sh'
        self.functions.write_text(RUNNER.read_text().split('exec 9>')[0])
    def tearDown(self):
        self.tmp.cleanup()
    def tool(self, name, body):
        p=self.bin/name
        p.write_text('#!/bin/bash\n'+body+'\n')
        p.chmod(0o700)
    def run_functions(self, body):
        return subprocess.run(['bash','-c','source "$1" "${@:2}"\n'+body,
                               'test',str(self.functions),*self.args], env=self.env,
                               capture_output=True, text=True)
    def test_missing_login_never_touches_services_or_clusters(self):
        touched=self.root/'touched'
        for tool in ('kind','k3d','systemctl','tilt'):
            self.tool(tool, f'touch "{touched}"; exit 1')
        (self.root/'vm.env').write_text("QA_CLUSTER_DRIVER=kind\nQA_STOP_SERVICES='dev.service'\n")
        result=subprocess.run(['bash',str(RUNNER),*self.args],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(touched.exists())
        manifest=json.loads((self.job/'out/remote-manifest.json').read_text())
        self.assertFalse(manifest['codex']['ran'])
        self.assertIn('needs login',manifest['environment']['reason'])
        self.assertEqual((self.job/'status').read_text().strip(),'failed')
    def test_occupied_port_is_rejected(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            port=listener.getsockname()[1]
            result=self.run_functions(f'TILT_PORT={port}\nQA_STOP_SERVICES=""\nprepare_exclusive_environment')
        self.assertNotEqual(result.returncode,0)
        self.assertIn(f'Port {port} is occupied',result.stderr)
    def test_existing_cluster_is_stopped_not_deleted(self):
        calls=self.root/'docker-calls'
        records=[{'Id':'kind-node','Name':'/zora-control-plane','Config':{'Labels':{'io.x-k8s.kind.cluster':'zora'}}},
                 {'Id':'app','Name':'/unrelated','Config':{'Labels':{}}},
                 {'Id':'k3d-node','Name':'/k3d-old-server-0','Config':{'Labels':{'k3d.cluster':'old'}}}]
        fixture=self.root/'containers.json'; fixture.write_text(json.dumps(records))
        self.tool('docker', f'''printf '%s\\n' "$*" >> "{calls}"
case "$1" in
 ps) echo 'kind-node app k3d-node' ;;
 inspect) cat "{fixture}" ;;
 stop) exit 0 ;;
 *) exit 1 ;;
esac''')
        result=self.run_functions('stop_existing_clusters')
        self.assertEqual(result.returncode,0,result.stderr)
        log=calls.read_text()
        self.assertIn('stop --time 30 kind-node',log)
        self.assertIn('stop --time 30 k3d-node',log)
        self.assertNotIn('stop --time 30 app',log)
        self.assertNotIn('rm ',log)
    def test_cleanup_never_restarts_old_environment(self):
        calls=self.root/'systemctl-calls'
        self.tool('systemctl',f'printf "%s\\n" "$*" >> "{calls}"')
        result=self.run_functions('LOCKED=true\nfinalize')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(calls.exists())
    def test_kind_mounts_only_job_data_and_uses_private_kubeconfig(self):
        calls=self.root/'kind-call'
        self.tool('kind',f'printf "%s\\n" "$@" > "{calls}"')
        result=self.run_functions('QA_CLUSTER_DRIVER=kind\ncreate_cluster')
        self.assertEqual(result.returncode,0,result.stderr)
        config=json.loads((self.job/'kind.json').read_text())
        mount=config['nodes'][0]['extraMounts'][0]
        self.assertEqual(mount['hostPath'],str(self.job/'work/tilt/data'))
        self.assertEqual(mount['containerPath'],'/mnt/mac'+str(self.job/'work/tilt/data'))
        self.assertIn(str(self.job/'kubeconfig'),calls.read_text())
        self.assertIn('zora-qa-regression',calls.read_text())

    def run_with(self, args, body):
        return subprocess.run(['bash','-c','source "$1" "${@:2}"\n'+body,
                               'test',str(self.functions),*args], env=self.env,
                               capture_output=True, text=True)
    def mock_tilt(self):
        calls=self.root/'tilt-call'
        self.tool('tilt', f'''printf "%s\\n" "$@" > "{calls}"
if [ -n "${{TILT_PROFILE+x}}" ]; then echo "set:$TILT_PROFILE" > "{calls}.env"; else echo unset > "{calls}.env"; fi''')
        (self.job/'work').mkdir(exist_ok=True)
        return calls
    def test_service_list_reaches_tilt_without_a_profile(self):
        calls=self.mock_tilt()
        args=['--job','regression','--dir',str(self.job),'--repo','unused','--base','abc',
              '--commit','abc','--services','api-gateway user loan-application task']
        result=self.run_with(args,'start_tilt; wait')
        self.assertEqual(result.returncode,0,result.stderr)
        argv=calls.read_text().split()
        self.assertIn('--',argv)
        self.assertEqual(argv[argv.index('--')+1:],['api-gateway','user','loan-application','task'])
        self.assertEqual((self.root/'tilt-call.env').read_text().strip(),'unset')
    def test_profile_reaches_tilt_as_environment(self):
        calls=self.mock_tilt()
        result=self.run_functions('start_tilt; wait')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('--',calls.read_text().split())
        self.assertEqual((self.root/'tilt-call.env').read_text().strip(),'set:infrastructure')
    def test_runner_requires_a_profile_or_services(self):
        args=['--job','regression','--dir',str(self.job),'--repo','unused','--base','abc','--commit','abc']
        result=subprocess.run(['bash',str(RUNNER),*args],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('need --profile or --services',result.stderr)

if __name__ == '__main__':
    unittest.main()
