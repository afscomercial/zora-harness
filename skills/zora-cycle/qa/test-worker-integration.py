#!/usr/bin/env python3
"""Opt-in root/Linux supervisor integration with real private Docker sandboxes.

The runner is synthetic: no application, Tilt or Codex QA is claimed. Every helper
is the actual bundled implementation. All state lives in a unique temporary QA home.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

RUNNER = '''#!/usr/bin/env bash
set -eu
python3 - "$(cd "$(dirname "$0")/.." && pwd)" <<'FIXTURE'
import json,os,pathlib,subprocess,sys,time
p=pathlib.Path(sys.argv[1])
with (p/'launches').open('a') as f: f.write(str(os.getpid())+'\\n')
child=subprocess.Popen(['sleep','600'])
(p/'child-inner.pid').write_text(str(child.pid))
(p/'runner-observed.json').write_text(json.dumps({'cgroup':pathlib.Path('/proc/self/cgroup').read_text(),
    'docker_id':subprocess.check_output(['docker','info','--format','{{.ID}}'],text=True).strip()}))
while not (p/'finish').exists(): time.sleep(.05)
child.terminate(); child.wait()
out=p/'out'; out.mkdir(exist_ok=True)
(out/'evidence').mkdir(exist_ok=True)
(out/'evidence/synthetic.txt').write_text('Supervisor fixture only; no application QA.\\n')
m=json.loads((p/'in/dispatch.json').read_text())
m.update(runner_version=5,environment={'ready':True,'sandbox_evidence':'evidence/0-sandbox.json'},codex={'ran':True,'exit_code':0},files={})
(out/'remote-manifest.json').write_text(json.dumps(m))
(p/'runner-status').write_text('done\\n')
FIXTURE
'''


def run(args, timeout=120):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


def wait(check, label, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(.2)
    raise AssertionError('timeout: ' + label)


def active(unit):
    result = subprocess.run(['systemctl', 'is-active', unit], text=True, capture_output=True)
    return result.stdout.strip() in ('active', 'activating', 'deactivating')


def assert_empty_group(identity):
    group = Path('/sys/fs/cgroup') / identity['cgroup'].lstrip('/')
    # Identity records the aggregate slice so all Docker children and exec scopes count.
    if group.exists():
        assert all(not f.read_text().strip() for f in group.rglob('cgroup.procs')), 'sandbox descendants survived'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--subnet-base', default='10.199')
    args = parser.parse_args()
    assert os.geteuid() == 0 and Path('/run/systemd/system').exists()
    source = Path(args.worker).resolve().parent
    helper_bytes = {name: (source / name).read_bytes() for name in ('qa-sandbox.sh', 'qa-network.sh')}
    home = Path(tempfile.mkdtemp(prefix='qa-worker-integration-', dir='/tmp'))
    worker = home / 'qa-worker.py'
    shutil.copyfile(args.worker, worker)
    (home / 'worker.json').write_text(json.dumps(dict(
        protocol_version=5, worker_id='integration', slots=1, isolation='sandbox', driver='kind',
        subnet_base=args.subnet_base, poll_seconds=.2, queue_timeout=600, run_timeout=600,
        cleanup_timeout=120, min_disk_gb=1, host_reserve_mb=128, slot_memory_mb=2048,
        process_memory_max_mb=1024, cluster_memory_max_mb=1024, max_load_per_cpu=100)))
    os.environ['QA_HOME'] = str(home)
    (home / 'empty-auth').mkdir()
    os.environ['QA_AUTH_HOME'] = str(home / 'empty-auth')
    attempts = []

    def cmd(action, target=None):
        return run(['python3', str(worker), '--home', str(home), action] + ([str(target)] if target else []))

    def new():
        ident = 'it-' + uuid.uuid4().hex
        path = home / 'jobs' / ident
        bundle = path / 'in'
        bundle.mkdir(parents=True)
        files = helper_bytes | {'qa-job.sh': RUNNER.encode(), 'qa-charter.md': b'Synthetic supervisor integration only.\n'}
        for name, data in files.items():
            (bundle / name).write_bytes(data)
        digest = hashlib.sha256(''.join(name + '\0' + hashlib.sha256(data).hexdigest() + '\n'
                                       for name, data in sorted(files.items())).encode()).hexdigest()
        dispatch = dict(protocol_version=5, execution_isolation='sandbox', worker_id='integration',
                        job_id=ident, attempt_id=ident, environment_id=ident, base='a'*40, commit='b'*40,
                        repo='https://example.invalid/repo', profile='infra', services=[],
                        charter_sha256=hashlib.sha256(files['qa-charter.md']).hexdigest(), bundle_sha256=digest)
        (bundle / 'dispatch.json').write_text(json.dumps(dispatch))
        attempts.append(path)
        return path

    def started(path):
        wait(lambda: (path / 'runner-observed.json').exists(), 'runner in real sandbox ' + path.name)
        identity = json.loads((path / 'sandbox.json').read_text())
        observed = json.loads((path / 'runner-observed.json').read_text())
        assert identity['daemon_id'] == observed['docker_id']
        assert identity['slice'] in observed['cgroup']
        assert json.loads((home / 'slots/1.json').read_text())['attempt_id'] == path.name
        return identity

    def stopped(path, identity):
        assert not active(identity['unit'])
        assert not active(identity['slice'])
        assert_empty_group(identity)
        assert not (path / 'sandbox').exists(), 'private Docker storage remains after clean teardown'
        namespace = 'qa-' + hashlib.sha256(path.name.encode()).hexdigest()[:10]
        assert namespace not in run(['ip', 'netns', 'list']).split(), 'network namespace leaked'
        assert json.loads((path / 'cleanup.json').read_text())['state'] == 'clean'

    def finish(path, identity):
        (path / 'finish').touch()
        wait(lambda: cmd('status', path.name) == 'done', 'terminal archive')
        with tarfile.open(path / 'result.tar.gz') as archive:
            manifest = json.load(archive.extractfile('remote-manifest.json'))
            assert manifest['cleanup']['state'] == 'clean', manifest
            assert manifest['sandbox']['daemon_id'] == identity['daemon_id']
            for name in ('evidence/0-cleanup.json', 'evidence/0-sandbox.json'):
                assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == manifest['files'][name]
        stopped(path, identity)

    succeeded = False
    try:
        a, b, cancelled, e = new(), new(), new(), new()
        cmd('submit', a)
        identity_a = started(a)
        cmd('submit', a)
        assert len((a / 'launches').read_text().splitlines()) == 1
        cmd('submit', b)
        cmd('submit', cancelled)
        cmd('submit', e)
        assert cmd('status', b.name) == 'queued' and not (b / 'launches').exists()
        assert cmd('cancel', cancelled.name) == 'cancelled'
        assert (cancelled / 'result.tar.gz').exists() and not (cancelled / 'launches').exists()
        finish(a, identity_a)
        identity_b = started(b)
        assert cmd('status', e.name) == 'queued' and not (e / 'launches').exists(), 'FIFO was violated'
        finish(b, identity_b)
        identity_e = started(e)
        finish(e, identity_e)
        print('PASS real sandbox idempotence, FIFO, queued cancellation, teardown-before-terminal archives', flush=True)

        crash = new()
        cmd('submit', crash)
        identity_crash = started(crash)
        supervisor = 'zora-qa-' + crash.name + '.service'
        run(['systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL', supervisor])
        wait(lambda: not active(supervisor), 'supervisor inactive')
        # Sandbox has a separate slice. Ownership must survive a supervisor crash.
        assert (home / 'slots/1.json').exists(), 'slot was freed before sandbox cleanup'
        cmd('reap')
        assert cmd('status', crash.name) == 'failed' and (crash / 'result.tar.gz').exists()
        stopped(crash, identity_crash)
        assert not list((home / 'slots').glob('*.json'))
        print('PASS supervisor SIGKILL, sandbox descendant cleanup, reaped archive, retained ownership until down', flush=True)

        running_cancel = new()
        cmd('submit', running_cancel)
        identity_cancel = started(running_cancel)
        assert cmd('cancel', running_cancel.name) == 'cancelled'
        stopped(running_cancel, identity_cancel)
        assert not list((home / 'slots').glob('*.json'))
        assert (running_cancel / 'result.tar.gz').exists()
        print('PASS running cancellation cleans separate sandbox slice before slot reuse', flush=True)
        succeeded = True
    finally:
        for path in attempts:
            subprocess.run(['systemctl', 'stop', 'zora-qa-' + path.name + '.service'], capture_output=True, timeout=45)
        try:
            cmd('reap')
        except Exception as exc:
            print('Cleanup review required:', exc)
        if list((home / 'slots').glob('*.json')) or not succeeded:
            print('Retained test evidence home:', home, flush=True)
        else:
            shutil.rmtree(home)


if __name__ == '__main__':
    main()
