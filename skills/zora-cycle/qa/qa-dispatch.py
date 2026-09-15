#!/usr/bin/env python3
"""Durable SSH dispatch; reconnect commands use persisted routing, never defaults."""
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid

HERE = Path(__file__).resolve().parent
BUNDLE = ('qa-job.sh', 'qa-network.sh', 'codex-qa-prompt.md', 'verdict.schema.json',
          'seed-baseline.cjs', 'qa-browser.cjs', 'redact-evidence.py')


def canonical_hash(files):
    return hashlib.sha256(''.join(name + '\0' + hashlib.sha256(data).hexdigest() + '\n'
                                  for name, data in sorted(files.items())).encode()).hexdigest()


def staging_supported(commit, repo=None):
    """Inspect the frozen commit, never mutable checkout contents."""
    result = subprocess.run(['git', 'show', commit + ':tilt/Tiltfile'], cwd=repo,
                            capture_output=True, check=False)
    return result.returncode == 0 and b'ZORA_TILT_STAGING_ROOT' in result.stdout


def worker_selection(selected):
    inventory = os.environ.get('ZORA_QA_WORKERS_FILE')
    if inventory:
        data = json.loads(Path(inventory).read_text())
        workers = data['workers']
        selected = selected or data.get('default_worker')
        if not selected and len(workers) == 1:
            selected = workers[0]['id']
        matches = [w for w in workers if w['id'] == selected]
        if len(matches) != 1:
            raise ValueError('select one configured worker with --worker')
        worker = matches[0]
    else:
        if selected and selected != 'vps-1':
            raise ValueError('worker inventory required for workers other than vps-1')
        worker = dict(id='vps-1', host=os.environ.get('ZORA_QA_HOST', ''),
                      home=os.environ.get('ZORA_QA_REMOTE_HOME', ''))
    validate_route(worker['host'], worker['home'])
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', worker['id']):
        raise ValueError('invalid worker id')
    return worker


def validate_route(host, home):
    if not host or host.startswith('-') or any(c.isspace() for c in host):
        raise ValueError('worker host must be an SSH destination')
    if not re.fullmatch(r'/[A-Za-z0-9._/-]+', home) or '..' in Path(home).parts:
        raise ValueError('worker home must be an absolute plain path')


def ssh(dispatch, command, data=None, output=None):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                           '-o', 'ServerAliveInterval=30', dispatch['host'], command],
                          input=data, stdout=output or subprocess.PIPE, check=True).stdout


def worker_command(d, action):
    home = d['remote_home']
    target = d['remote_dir'] if action == 'submit' else d['attempt_id']
    return shlex.join(['python3', home + '/qa-worker.py', '--home', home, action, target])


def status(d):
    if d.get('protocol_version') == 4:
        return ssh(d, worker_command(d, 'status')).decode().strip()
    return ssh(d, 'cat ' + shlex.quote(d['remote_dir'] + '/status')).decode().strip()


def collect(directory, d):
    state = status(d)
    if state not in ('done', 'failed', 'cancelled'):
        raise ValueError(f'attempt is {state}; collection does not start another execution')
    # Keep the inode persistent: unlinking a lock file would let later callers lock
    # a different inode while a waiting collector still owns the original one.
    with (directory / '.collection.lock').open('a+') as collection_lock:
        fcntl.flock(collection_lock, fcntl.LOCK_EX)
        try:
            _collect_locked(directory, d, state)
        finally:
            fcntl.flock(collection_lock, fcntl.LOCK_UN)


def _collect_locked(directory, d, state):
    download = directory / '.download'
    download.mkdir(exist_ok=True)
    archive = download / 'result.tar.gz'
    with archive.open('wb') as out:
        ssh(d, 'cat ' + shlex.quote(d['remote_dir'] + '/result.tar.gz'), output=out)
    # Extract into an empty directory, preventing preexisting symlinks from redirecting writes.
    import tempfile
    import shutil
    with tempfile.TemporaryDirectory(dir=download) as stage:
        subprocess.run(['bash', str(HERE / 'run-codex-qa'), '--extract', str(archive), stage], check=True)
        for child in Path(stage).iterdir():
            target = directory / child.name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            shutil.move(str(child), str(target))
    (directory / 'collection.json').write_text(json.dumps({'status': state, 'collected_at': time.time()}))
    print(f'RESULTS    {directory}', flush=True)
    print(f'NEXT       bash {shlex.quote(str(HERE.parent / "verdict-check.sh"))} {shlex.quote(str(directory))} --remote')


def reconnect(action, path):
    directory = Path(path).resolve()
    d = json.loads((directory / 'dispatch.json').read_text())
    if d.get('protocol_version') != 4:
        if action == '--cancel':
            raise ValueError('legacy jobs cannot be safely cancelled through the worker supervisor')
        home = str(Path(d['remote_dir']).parent.parent)
    else:
        home = d['remote_home']
    validate_route(d['host'], home)
    if not d['remote_dir'].startswith(home + '/jobs/'):
        raise ValueError('remote attempt directory is outside the recorded worker')
    if action == '--collect':
        collect(directory, d)
    elif action == '--status':
        print(status(d))
    else:
        print(ssh(d, worker_command(d, 'cancel')).decode().strip())


def submit(args):
    worker = worker_selection(args.worker)
    job, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    directory = Path(args.run) / 'qa' / job / 'attempts' / attempt
    directory.mkdir(parents=True)
    files = {name: (HERE / name).read_bytes() for name in BUNDLE}
    files['qa-charter.md'] = (Path(args.run) / 'qa-charter.md').read_bytes()
    d = dict(protocol_version=4, job_id=job, attempt_id=attempt, environment_id=attempt,
             worker_id=worker['id'], host=worker['host'], remote_home=worker['home'],
             remote_dir=worker['home'] + '/jobs/' + attempt, base=args.base,
             commit=args.commit, repo=args.repo, profile=args.profile or None,
             services=args.services.split(), charter_sha256=hashlib.sha256(files['qa-charter.md']).hexdigest(),
             bundle_sha256=canonical_hash(files), requested_at=time.time(),
             staging_isolation_supported=staging_supported(args.commit))
    encoded = json.dumps(d, indent=2).encode()
    expected = directory / 'dispatch.json'
    with expected.open('xb') as out:
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    print(f'ATTEMPT    {directory}', flush=True)
    if args.dry == '1':
        print('DRY RUN    preflight passed; no worker contacted')
        return
    files['dispatch.json'] = encoded
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode='w:gz') as tar:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            tar.addfile(member, io.BytesIO(data))
    remote = shlex.quote(d['remote_dir'])
    stage = shlex.quote(d['remote_dir'] + '/.upload-' + str(uuid.uuid4()))
    # flock protects atomic publication against duplicate submissions of this attempt.
    upload = (f'mkdir -p {remote} && mkdir {stage} && tar -xzf - -C {stage} && '
              f'flock {remote}/upload.lock sh -c ' + shlex.quote(
                  f'if [ -d {remote}/in ]; then rm -rf {stage}; else mv {stage} {remote}/in; fi'))
    ssh(d, upload, bundle.getvalue())
    try:
        ssh(d, worker_command(d, 'submit'))
    except subprocess.CalledProcessError:
        print('Submission response was lost; reconciling the same attempt.', file=sys.stderr)
        print(status(d))
        raise ValueError('use --status/--collect with the ATTEMPT path; do not redispatch')
    deadline = time.monotonic() + int(os.environ.get('ZORA_QA_TIMEOUT') or 10800)
    last = None
    while True:
        state = status(d)
        if state != last:
            print(state, flush=True)
            last = state
        if state in ('done', 'failed', 'cancelled'):
            collect(directory, d)
            return
        if time.monotonic() >= deadline:
            raise ValueError('local wait expired; attempt continues. Use --status/--collect with its path')
        time.sleep(min(60, int(os.environ.get('ZORA_QA_POLL') or 30)))


def main():
    if len(sys.argv) == 3 and sys.argv[1] in ('--collect', '--status', '--cancel'):
        reconnect(*sys.argv[1:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['submit'])
    for name in ('run', 'base', 'commit', 'profile', 'services', 'repo', 'worker', 'dry'):
        parser.add_argument('--' + name, required=True)
    submit(parser.parse_args())


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'FAILED     {exc}; persisted attempt metadata remains available for reconnection', file=sys.stderr)
        sys.exit(3)
