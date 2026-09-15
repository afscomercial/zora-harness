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
        self.env = dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'], QA_MANAGED='1', QA_SANDBOX='1', QA_SLOT_ID='1', QA_NET_ISOLATION='netns', QA_CLUSTER_MEMORY_MAX_MB='5000')
        (self.job/'in/qa-sandbox.sh').write_text('exit 0')
        (self.job/'in/dispatch.json').write_text(json.dumps(dict(protocol_version=5,job_id='logical',attempt_id='regression',worker_id='vps-1',environment_id='regression',charter_sha256='c'*64,bundle_sha256='b'*64)))
        (self.job/'in/redact-evidence.py').write_text('')
        self.args = ['--job','regression','--dir',str(self.job),'--repo','unused',
                     '--base','abc','--commit','abc','--profile','infrastructure']
        self.tool('docker', 'exit 0')
        self.tool('kubectl', 'exit 0')
        self.tool('codex', 'if [ "$1" = --version ]; then echo mock; exit 0; fi\nexit 1')
        self.functions = self.root/'functions.sh'
        self.functions.write_text(RUNNER.read_text().split('# --- entrypoint ---')[0])
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
        self.assertEqual((self.job/'runner-status').read_text().strip(),'failed')
    def test_occupied_port_is_rejected(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0)); listener.listen()
            port=listener.getsockname()[1]
            result=self.run_functions(f'TILT_PORT={port}\nQA_STOP_SERVICES=""\ncheck_private_ports')
        self.assertNotEqual(result.returncode,0)
        self.assertIn(f'Port {port} is occupied',result.stderr)
    def test_direct_host_execution_is_rejected(self):
        self.env.pop('QA_SANDBOX')
        result=subprocess.run(['bash',str(RUNNER),*self.args],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('requires a supervised private sandbox',result.stderr)

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
        self.assertIn('zora-qa-',calls.read_text())

    def test_managed_prepare_does_not_stop_shared_resources(self):
        calls=self.root/'shared-mutation'
        for tool in ('docker','systemctl'):
            self.tool(tool, f'touch "{calls}"; exit 1')
        result=self.run_functions('QA_MANAGED=1\ncheck_private_ports')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(calls.exists())

    def test_kind_uses_private_loopback_and_checks_actual_kubeconfig(self):
        calls=self.root/'kubectl-calls'
        self.tool('kind', 'exit 0')
        self.tool('nsenter', 'shift; exec \"$@\"')
        self.tool('kubectl',f'printf "%s\\n" "$*" > "{calls}"')
        self.env.update(QA_MANAGED='1', QA_NET_ISOLATION='netns', QA_HOST_IP='10.203.0.1',
                        QA_STAGING_ROOT=str(self.job/'staging'))
        result=self.run_functions('create_cluster')
        self.assertEqual(result.returncode,0,result.stderr)
        config=json.loads((self.job/'kind.json').read_text())
        self.assertEqual(config['networking']['apiServerAddress'],'127.0.0.1')
        self.assertNotIn('apiServerPort',config['networking'])
        self.assertIn(str(self.job/'kubeconfig'),calls.read_text())
        self.assertIn('get --raw=/readyz',calls.read_text())

    def test_managed_vm_settings_do_not_override_identity(self):
        (self.root/'vm.env').write_text('QA_AUTH_FILE=/wrong\nQA_SLOT_ID=wrong\n')
        self.env.update(QA_MANAGED='1',QA_STAGING_ROOT=str(self.job/'staging'),
                        QA_AUTH_FILE='/correct',QA_SLOT_ID='slot-1')
        result=self.run_functions('printf "%s %s" "$QA_AUTH_FILE" "$QA_SLOT_ID"')
        self.assertEqual(result.stdout,'/correct slot-1')

    def test_managed_finalize_waits_for_supervisor_and_binds_manifest(self):
        dispatch=dict(protocol_version=5,job_id='logical',attempt_id='regression',
                      worker_id='vps-1',environment_id='regression',
                      charter_sha256='c'*64,bundle_sha256='b'*64)
        (self.job/'in/dispatch.json').write_text(json.dumps(dispatch))
        self.env.update(QA_MANAGED='1',QA_STAGING_ROOT=str(self.job/'staging'),
                        QA_SLOT_ID='slot-1',QA_NET_ISOLATION='netns')
        result=self.run_functions('LOCKED=true\nENV_READY=true\nCODEX_RAN=true\nfinalize')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.job/'status').read_text().strip(),'packaging')
        self.assertEqual((self.job/'runner-status').read_text().strip(),'done')
        manifest=json.loads((self.job/'out/remote-manifest.json').read_text())
        for key,value in dispatch.items():
            self.assertEqual(manifest[key],value)
        self.assertEqual(manifest['slot_id'],'slot-1')

    def test_long_attempts_have_distinct_cluster_names(self):
        self.env.update(QA_MANAGED='1',QA_STAGING_ROOT=str(self.job/'staging'))
        names=[]
        for ending in ('a','b'):
            args=self.args.copy(); args[1]='same-prefix-'*6+ending
            result=self.run_with(args,'printf %s "$QA_CLUSTER"')
            self.assertEqual(result.returncode,0,result.stderr)
            names.append(result.stdout)
        self.assertNotEqual(*names)
        self.assertTrue(all(len(name)<=35 for name in names))

    def run_network_cleanup(self, firewall_body):
        self.tool('ip', 'if [ "$1" = -j ]; then echo "[]"; fi')
        self.tool('iptables',firewall_body)
        self.env.update(QA_NS='qa-'+self.root.name,QA_HOST_IP='10.203.0.1',QA_PEER_IP='10.203.0.2')
        return subprocess.run(['bash',str(RUNNER.with_name('qa-network.sh')),'down'],
                              env=self.env,capture_output=True,text=True)

    def test_network_cleanup_missing_resources_is_idempotent(self):
        result=self.run_network_cleanup('if [[ "$*" = *"-S"* ]]; then exit 0; else exit 1; fi')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_network_cleanup_cannot_verify_firewall_fails(self):
        result=self.run_network_cleanup('exit 1')
        self.assertNotEqual(result.returncode,0)

    def test_network_cleanup_surviving_tagged_rule_fails(self):
        result=self.run_network_cleanup('if [[ "$*" = *"-S"* ]]; then echo "-A FORWARD -m comment --comment $QA_NS -j ACCEPT"; fi')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('firewall rules survived',result.stderr)

    def test_tilt_cli_state_is_private_and_helm_restores_job_kubeconfig(self):
        self.tool('kubectl','exit 0')
        self.tool('mongosh','exit 0')
        self.tool('helm','printf "%s" "$KUBECONFIG"')
        self.tool('tilt','printf "%s|%s|%s|%s" "$TILT_DEV_DIR" "$XDG_RUNTIME_DIR" "$XDG_CONFIG_HOME" "$HOME"')
        result=self.run_functions('install_command_wrappers\ntilt get uiresources\nprintf "\\n"\nKUBECONFIG=/deleted-frozen-config helm list')
        self.assertEqual(result.returncode,0,result.stderr)
        lines=result.stdout.splitlines()
        state=str(self.job/'scratch/tilt-state')
        self.assertEqual(lines[0],f'{state}/legacy|{state}/runtime|{state}/config|{self.env["HOME"]}')
        self.assertEqual(lines[1],str(self.job/'kubeconfig'))

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
    def test_finalize_removes_the_checkout(self):
        (self.job/'work'/'apps').mkdir(parents=True)
        (self.job/'work'/'apps'/'.env').write_text('SECRET=value\n')
        result=self.run_functions('LOCKED=true\nfinalize')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.job/'work').exists())
        self.assertFalse((self.job/'scratch').exists())
        self.assertTrue((self.job/'result.tar.gz').exists())
    def test_finalize_keeps_the_checkout_when_asked(self):
        (self.job/'work').mkdir()
        result=self.run_functions('LOCKED=true\nQA_KEEP_WORK=1\nfinalize')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.job/'work').exists())
    def test_invalid_sandbox_never_starts_runner(self):
        (self.job/'in/qa-sandbox.sh').write_text('exit 1')
        result=subprocess.run(['bash',str(RUNNER),*self.args],env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertFalse((self.job/'runner-status').exists())

if __name__ == '__main__':
    unittest.main()
