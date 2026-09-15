#!/usr/bin/env python3
"""Opt-in Linux/root proof of two complete QA sandboxes; never uses the host Docker daemon.

Run on a worker with >=8GiB spare memory:
  QA_SANDBOX_TEST_SUBNET=10.198.254.0/29 python3 test-sandbox-integration.py
Uses two 4GiB sandboxes. Logs and namespace identities stay in the printed /tmp directory.
"""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

IMAGE = os.environ.get('QA_SANDBOX_TEST_IMAGE',
    'busybox:1.37.0@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0')
SETUP = r'''set -euo pipefail
mkdir -p /tmp/tilt-dev-deps-context /tmp/tilt-pruned
printf '%s\n' "$1" > /tmp/tilt-dev-deps-context/marker
printf '%s\n' "$1" > /tmp/tilt-pruned/index.html
printf 'FROM %s\nCOPY marker /marker\n' "$2" > /tmp/tilt-dev-deps-context/Dockerfile
docker build -q -t sandbox-proof:identical /tmp/tilt-dev-deps-context
docker run -d --name identical -p 127.0.0.1:5173:5173 \
  --mount type=bind,src=/tmp/tilt-pruned,dst=/web sandbox-proof:identical httpd -f -p 5173 -h /web
pid=$(docker inspect identical --format '{{.State.Pid}}')
cat "/proc/$pid/cgroup"
'''


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this opt-in test as root on a Linux QA worker.')
    pool = ipaddress.ip_network(os.environ.get('QA_SANDBOX_TEST_SUBNET', '10.198.254.0/29'), strict=True)
    if pool.version != 4 or pool.prefixlen != 29:
        raise SystemExit('QA_SANDBOX_TEST_SUBNET must be an aligned IPv4 /29')
    root = Path(tempfile.mkdtemp(prefix='qa-sandbox-integration-', dir='/tmp'))
    print(f'Evidence: {root}', flush=True)
    source = Path(__file__).resolve().parent
    for name in ('qa-sandbox.sh', 'qa-network.sh'):
        shutil.copyfile(source / name, root / name)
    (root / 'empty-auth').mkdir()
    token = root.name.rsplit('-', 1)[-1].replace('_', 'x')
    attempts = [root / 'jobs' / f'proof-{token}-{i}' for i in range(2)]
    helper = root / 'qa-sandbox.sh'
    common = os.environ | {'QA_HOME': str(root), 'QA_AUTH_HOME': str(root / 'empty-auth'),
                           'QA_SANDBOX_MEMORY_MB': '4000'}
    log = (root / 'proof.log').open('w')

    def run(args, env=None, timeout=420, check=True):
        result = subprocess.run(args, env=env or common, text=True, capture_output=True, timeout=timeout)
        log.write(result.stdout + result.stderr)
        log.flush()
        if check and result.returncode:
            raise RuntimeError(f'{args[:3]} exited {result.returncode}: {result.stderr[-1500:]}')
        return result.stdout.strip(), result.returncode

    def execute(index, *args, check=True):
        return run(['bash', str(helper), 'exec', str(attempts[index]), *args], check=check)

    success = False
    try:
        identities = []
        for i, subnet in enumerate(pool.subnets(new_prefix=30)):
            attempts[i].mkdir(parents=True)
            host, peer = map(str, subnet.hosts())
            env = common | {'QA_NS': f'qa-{token}-{i}', 'QA_HOST_IP': host, 'QA_PEER_IP': peer}
            run(['bash', str(helper), 'start', str(attempts[i])], env=env)
            identity = json.loads((attempts[i] / 'sandbox.json').read_text())
            identities.append(identity)
            budget = Path('/sys/fs/cgroup' + identity['cgroup']) / 'memory.max'
            assert int(budget.read_text()) == 4000 * 1024 * 1024
            out, _ = execute(i, 'bash', '-c', SETUP, 'setup', f'environment-{i}', IMAGE)
            assert identity['cgroup'] + '/' in out, 'container escaped aggregate cgroup'
        assert identities[0]['daemon_id'] != identities[1]['daemon_id']
        for namespace in ('mount', 'pid', 'net', 'uts', 'ipc'):
            assert identities[0]['namespaces'][namespace] != identities[1]['namespaces'][namespace]
        for i in range(2):
            response, _ = execute(i, 'curl', '-fsS', '--retry', '5', '--retry-connrefused', '--max-time', '5', 'http://127.0.0.1:5173/')
            assert response == f'environment-{i}'
            marker, _ = execute(i, 'docker', 'exec', 'identical', 'cat', '/marker')
            assert marker == f'environment-{i}'
        before, _ = execute(1, 'docker', 'image', 'inspect', 'sandbox-proof:identical', '--format', '{{.Id}}')
        execute(0, 'bash', '-ec', 'printf "rebuilt\\n" > /tmp/tilt-dev-deps-context/marker; docker build -q -t sandbox-proof:identical /tmp/tilt-dev-deps-context; docker system prune -af; docker run --rm sandbox-proof:identical cat /marker')
        after, _ = execute(1, 'docker', 'image', 'inspect', 'sandbox-proof:identical', '--format', '{{.Id}}')
        assert before == after
        _, code = execute(1, 'env', 'DOCKER_HOST=unix:///var/run/docker.sock', 'bash', str(helper), 'check', str(attempts[1]), check=False)
        assert code != 0, 'host Docker endpoint was accepted'
        run(['systemctl', 'kill', '--kill-whom=main', '--signal=KILL', identities[0]['unit']])
        time.sleep(2)
        run(['bash', str(helper), 'down', str(attempts[0])])
        run(['bash', str(helper), 'verify', str(attempts[1])])
        response, _ = execute(1, 'curl', '-fsS', '--max-time', '5', 'http://127.0.0.1:5173/')
        assert response == 'environment-1'
        log.write('PASS: private daemons/filesystems/tags/ports, aggregate memory, rebuild/prune isolation, host endpoint rejection, crash isolation\n')
        success = True
    finally:
        for attempt in attempts:
            try:
                run(['bash', str(helper), 'down', str(attempt)], timeout=180)
            except Exception as exc:
                log.write(f'CLEANUP FAILURE {attempt}: {exc}\n')
                success = False
        log.write(f'cleanup_complete success={success}\n')
        log.close()
    if not success:
        raise SystemExit(f'Proof failed; inspect {root}/proof.log')
    print(f'PASS: {root}/proof.log', flush=True)


if __name__ == '__main__':
    main()
