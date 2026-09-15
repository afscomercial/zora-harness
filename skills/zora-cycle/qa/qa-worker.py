#!/usr/bin/env python3
"""Worker-local QA admission, supervision and recovery. Linux/systemd only; stdlib only."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

PROTOCOL = 4
ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
TERMINAL = {'done', 'failed', 'cancelled'}
DEFAULTS = {
    'protocol_version': 4, 'worker_id': 'vps-1', 'slots': 1,
    'isolation': 'none', 'driver': 'kind', 'subnet_base': '10.77',
    'queue_timeout': 7200, 'run_timeout': 14400, 'preparation_timeout': 5400, 'validation_timeout': 7200, 'cleanup_timeout': 240,
    'host_reserve_mb': 5000, 'slot_memory_mb': 13000, 'min_disk_gb': 60,
    'process_memory_max_mb': 6000, 'cluster_memory_max_mb': 7000,
    'max_load_per_cpu': 2.0, 'poll_seconds': 5, 'retention_seconds': 21600,
    'identity_bundles': {}, 'profiles': {},
}

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n' if isinstance(data, (dict, list)) else data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default

@contextlib.contextmanager
def lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
        else:
            try:
                yield file
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

def command(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=60, **kwargs)

def active(unit):
    result = command(['systemctl', 'show', unit, '--property=ActiveState', '--value'])
    return result.returncode == 0 and result.stdout.strip() in {'active', 'activating', 'deactivating', 'reloading'}

def unit_name(attempt):
    return f'zora-qa-{attempt}.service'

class Worker:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.cfg = DEFAULTS | read(self.home / 'worker.json', {})
        c = self.cfg
        if c['protocol_version'] != PROTOCOL or not ID.fullmatch(c['worker_id']):
            raise ValueError('unsupported worker protocol or worker ID')
        if not isinstance(c['slots'], int) or not 1 <= c['slots'] <= 250:
            raise ValueError('slots must fit the configured /24-per-slot address pool (1..250)')
        if c['isolation'] not in {'none', 'netns'} or (c['isolation'] == 'none' and c['slots'] != 1):
            raise ValueError('multiple slots require netns isolation')
        if c['driver'] != 'kind':
            raise ValueError('managed worker currently supports Kind only')
        octets = c['subnet_base'].split('.')
        if len(octets) != 2 or not all(x.isdigit() and 0 <= int(x) <= 255 for x in octets):
            raise ValueError('subnet_base must be two IPv4 octets')
        for key in ('queue_timeout', 'run_timeout', 'preparation_timeout', 'validation_timeout', 'cleanup_timeout', 'host_reserve_mb',
                    'slot_memory_mb', 'min_disk_gb', 'process_memory_max_mb',
                    'cluster_memory_max_mb', 'poll_seconds', 'retention_seconds'):
            if not isinstance(c[key], (int, float)) or c[key] <= 0:
                raise ValueError(f'{key} must be positive')
        minimum = c['process_memory_max_mb'] + c['cluster_memory_max_mb']
        if c['slot_memory_mb'] < minimum or any(not isinstance(v, (int, float)) or v < minimum for v in c['profiles'].values()):
            raise ValueError('slot/profile reservation must cover process and cluster budgets')
        self.jobs = self.home / 'jobs'
        self.slots = self.home / 'slots'
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.slots.mkdir(parents=True, exist_ok=True)
        self.guard = self.home / '.admission.lock'

    def directory(self, attempt):
        if not ID.fullmatch(attempt):
            raise ValueError('invalid attempt ID')
        path = self.jobs / attempt
        if path.resolve().parent != self.jobs.resolve() or path.is_symlink():
            raise ValueError('attempt must be a direct owned jobs directory')
        return path

    def dispatch(self, directory):
        path = Path(directory).resolve()
        if path != self.directory(path.name):
            raise ValueError('invalid attempt directory')
        d = read(path / 'in/dispatch.json')
        if not isinstance(d, dict) or d.get('protocol_version') != PROTOCOL:
            raise ValueError('unsupported dispatch protocol')
        for key in ('job_id', 'attempt_id', 'worker_id', 'environment_id'):
            if not isinstance(d.get(key), str) or not ID.fullmatch(d[key]):
                raise ValueError(f'invalid {key}')
        if d['worker_id'] != self.cfg['worker_id'] or d['attempt_id'] != path.name or d['environment_id'] != path.name:
            raise ValueError('dispatch worker/attempt/environment mismatch')
        for key in ('base', 'commit'):
            if not re.fullmatch('[a-f0-9]{40}', d.get(key, '')):
                raise ValueError(f'invalid {key}')
        files = sorted((path / 'in').iterdir(), key=lambda f: f.name)
        digest = hashlib.sha256()
        for file in files:
            if file.name == 'dispatch.json':
                continue
            if file.is_symlink() or not file.is_file():
                raise ValueError('bundle must contain regular flat files only')
            digest.update((file.name + '\0' + hashlib.sha256(file.read_bytes()).hexdigest() + '\n').encode())
        if digest.hexdigest() != d.get('bundle_sha256'):
            raise ValueError('bundle hash mismatch')
        if hashlib.sha256((path / 'in/qa-charter.md').read_bytes()).hexdigest() != d.get('charter_sha256'):
            raise ValueError('charter hash mismatch')
        if not isinstance(d.get('repo'), str) or not d['repo'] or d['repo'].startswith('-'):
            raise ValueError('invalid repository')
        services = d.get('services') or []
        profile = d.get('profile')
        if bool(services) == bool(profile):
            raise ValueError('provide exactly one profile or services')
        if not isinstance(services, list) or any(not re.fullmatch('[a-z0-9-]+', s) for s in services):
            raise ValueError('invalid services')
        if profile and not re.fullmatch('[a-z0-9-]+', profile):
            raise ValueError('invalid profile')
        return d

    def status(self, attempt):
        path = self.directory(attempt) / 'status'
        return path.read_text().strip() if path.exists() else 'missing'

    def set_status(self, path, state, reason=None):
        atomic(path / 'status', state + '\n')
        meta = read(path / 'worker-state.json', {})
        meta.update(status=state, updated_at=time.time())
        if reason:
            meta['reason'] = reason
        atomic(path / 'worker-state.json', meta)

    def submit(self, directory):
        d = self.dispatch(directory)
        path = self.directory(d['attempt_id'])
        with lock(self.guard):
            state = read(path / 'worker-state.json')
            if state:
                if state.get('bundle_sha256') != d['bundle_sha256']:
                    raise ValueError('attempt already exists with different bundle')
                if self.status(path.name) != 'queued' or active(unit_name(path.name)):
                    return self.status(path.name)
            else:
                atomic(path / 'worker-state.json', {'submitted_at': time.time(), 'bundle_sha256': d['bundle_sha256']})
                self.set_status(path, 'queued')
            account = pwd.getpwuid(os.getuid())
            args = ['systemd-run', '--quiet', '--collect', '--unit=' + unit_name(path.name),
                    '--setenv=HOME=' + account.pw_dir, '--setenv=USER=' + account.pw_name,
                    '--setenv=LOGNAME=' + account.pw_name,
                    '--property=Type=exec', '--property=KillMode=control-group',
                    '--property=TimeoutStopSec=30', '--property=UMask=0077',
                    '--property=StandardOutput=append:' + str(path / 'supervisor.log'),
                    '--property=StandardError=append:' + str(path / 'supervisor.log'),
                    sys.executable, str(Path(__file__).resolve()), '--home', str(self.home), 'execute', path.name]
            result = command(args)
            if result.returncode:
                raise RuntimeError('could not start QA unit: ' + result.stderr)
        return 'queued'

    def memory(self):
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            values[key] = int(value.strip().split()[0]) // 1024
        return values

    def records(self):
        return [read(p) for p in self.slots.glob('*.json')]

    def admission(self, budget):
        mem = self.memory()
        reservations = sum(r['memory_mb'] for r in self.records() if r)
        reserve = self.cfg['host_reserve_mb']
        reasons = []
        if any(r.get('exclusive') for r in self.records() if r):
            reasons.append('exclusive legacy environment')
        if reservations + budget + reserve > mem['MemTotal']:
            reasons.append('reserved memory')
        if mem['MemAvailable'] < budget + reserve:
            reasons.append('available memory')
        if shutil.disk_usage(self.home).free < self.cfg['min_disk_gb'] * (1 << 30):
            reasons.append('disk headroom')
        if os.getloadavg()[0] / (os.cpu_count() or 1) > self.cfg['max_load_per_cpu']:
            reasons.append('CPU load')
        return reasons

    def earlier_waiter(self, path):
        state = read(path / 'worker-state.json', {})
        own = (state.get('submitted_at', 0), path.name)
        for other in self.jobs.iterdir():
            if other == path or not other.is_dir() or self.status(other.name) != 'queued':
                continue
            entry = read(other / 'worker-state.json', {})
            if (entry.get('submitted_at', 0), other.name) < own and active(unit_name(other.name)):
                return True
        return False

    def environment(self, path, slot, d):
        identity = self.cfg['identity_bundles'].get(str(slot), {})
        env = {
            'QA_MANAGED': '1', 'QA_SLOT_ID': str(slot), 'QA_WORKER_ID': d['worker_id'],
            'QA_ENVIRONMENT_ID': d['environment_id'], 'QA_JOB_ID': d['job_id'],
            'QA_ATTEMPT_ID': d['attempt_id'], 'QA_NET_ISOLATION': self.cfg['isolation'],
            'QA_SLOTS': str(self.cfg['slots']), 'QA_CLUSTER_DRIVER': 'kind',
            'QA_NS': 'qa-' + hashlib.sha256(d['attempt_id'].encode()).hexdigest()[:10],
            'QA_HOST_IP': f"{self.cfg['subnet_base']}.{slot}.1",
            'QA_PEER_IP': f"{self.cfg['subnet_base']}.{slot}.2",
            'QA_STAGING_ROOT': str(path / 'staging'),
            'QA_CLUSTER_MEMORY_MAX_MB': str(self.cfg['cluster_memory_max_mb']),
            'QA_AUTH_FILE': identity.get('auth_file', str(path / 'unconfigured-auth.json')),
            'QA_IDENTITY_FILE': identity.get('identity_file', str(path / 'unconfigured-identity.json')),
            'QA_QUEUED_SECONDS': str(int(time.time() - read(path / 'worker-state.json')['submitted_at'])),
        }
        if self.cfg['slots'] > 1 and identity:
            for other_slot, bundle in self.cfg['identity_bundles'].items():
                if other_slot != str(slot) and bundle.get('auth_file') == identity.get('auth_file'):
                    if not self.cfg.get('shared_identity_concurrency_verified', False):
                        raise ValueError('shared concurrent identity has not been verified')
        return env

    def execute(self, attempt):
        path = self.directory(attempt)
        d = self.dispatch(path)
        with lock(path / '.execute.lock', False) as singleton:
            if singleton is None or self.status(attempt) in TERMINAL:
                return
            try:
                self._execute(path, d)
            except Exception as exc:
                print(f'worker exception: {exc}', flush=True)
                self.failure(path, d, str(exc))
                raise

    def _execute(self, path, d):
        profile_budget = self.cfg['profiles'].get(d.get('profile'), self.cfg['slot_memory_mb'])
        exclusive = d.get('staging_isolation_supported') is not True
        deadline = read(path / 'worker-state.json')['submitted_at'] + self.cfg['queue_timeout']
        slot_file = None
        record = None
        while time.time() < deadline:
            with lock(self.guard):
                reasons = self.admission(profile_budget)
                if exclusive and self.records(): reasons.append('legacy commit requires all slots idle')
                if not reasons and not self.earlier_waiter(path):
                    for slot in range(1, self.cfg['slots'] + 1):
                        if (self.slots / f'{slot}.json').exists():
                            continue
                        handle = (self.slots / f'{slot}.lock').open('a+')
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            handle.close()
                            continue
                        slot_file = handle
                        env = self.environment(path, slot, d)
                        if exclusive:
                            env.update(QA_NET_ISOLATION='none', QA_SLOTS='1')
                        record = {'attempt_id': path.name, 'slot_id': slot, 'memory_mb': profile_budget,
                                  'unit': unit_name(path.name), 'created_at': time.time(), 'state': 'reserved', 'exclusive': exclusive,
                                  'env': env}
                        atomic(self.slots / f'{slot}.json', record)
                        atomic(path / 'ownership.json', record)
                        self.set_status(path, 'preparing')
                        break
                if record:
                    break
                atomic(path / 'admission.json', {'at': time.time(), 'reasons': reasons or ['queue or slots']})
            time.sleep(self.cfg['poll_seconds'])
        if not record:
            self.failure(path, d, 'capacity queue timeout; Codex did not run')
            return
        try:
            process_limit = self.cfg['process_memory_max_mb']
            result = command(['systemctl', 'set-property', '--runtime', unit_name(path.name),
                              f'MemoryMax={process_limit}M', 'MemoryAccounting=yes', 'CPUAccounting=yes'])
            if result.returncode:
                raise RuntimeError('could not apply process resource budget: ' + result.stderr)
            args = ['bash', str(path / 'in/qa-job.sh'), '--job', path.name, '--dir', str(path),
                    '--repo', d['repo'], '--base', d['base'], '--commit', d['commit']]
            args += ['--services', ' '.join(d['services'])] if d.get('services') else ['--profile', d['profile']]
            with (path / 'job.log').open('a') as output:
                try:
                    launch = ['bash', '-c', 'set -a; [ ! -f "$1" ] || source "$1"; shift; exec env "$@"', 'qa-managed', str(self.home / 'vm.env')] + [k + '=' + v for k, v in record['env'].items()] + args
                    process = subprocess.Popen(launch, stdout=output, stderr=subprocess.STDOUT, close_fds=True)
                    phase, phase_start, started = 'preparing', time.monotonic(), time.monotonic()
                    while process.poll() is None:
                        now = time.monotonic()
                        state = self.status(path.name)
                        if state != phase:
                            phase, phase_start = state, now
                        phase_limit = self.cfg['validation_timeout'] if phase == 'validating' else self.cfg['preparation_timeout']
                        if now - started > self.cfg['run_timeout'] or now - phase_start > phase_limit:
                            process.kill()
                            process.wait()
                            raise subprocess.TimeoutExpired(launch, now - started)
                        time.sleep(1)
                    result = subprocess.CompletedProcess(launch, process.returncode)
                except subprocess.TimeoutExpired:
                    self.failure(path, d, 'execution deadline exceeded')
                else:
                    if result.returncode or not (path / 'result.tar.gz').exists():
                        self.failure(path, d, f'runner exited {result.returncode} without a complete result')
            self.telemetry(path, record)
        finally:
            self.cleanup(record)
            slot_file.close()
            self.package(path, d)

    def sanitize(self, path):
        helper = path / 'in/redact-evidence.py'
        if not helper.exists():
            # Synthetic worker tests have no secret-producing runner.
            return
        result = subprocess.run([sys.executable, str(helper), str(path / 'out'), str(self.home)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            quarantine = path / ('unredacted-out-' + str(time.time_ns()))
            (path / 'out').rename(quarantine)
            (path / 'out').mkdir(mode=0o700)
            (path / 'result.tar.gz').unlink(missing_ok=True)
            raise RuntimeError('evidence redaction failed; raw evidence retained only on worker')

    def package(self, path, d):
        import tarfile
        out = path / 'out'
        manifest = read(out / 'remote-manifest.json')
        if not manifest:
            self.failure(path, d, 'runner did not emit manifest')
            return
        self.sanitize(path)
        evidence = out / 'evidence'
        evidence.mkdir(exist_ok=True)
        for name in ('worker-telemetry.json', 'cleanup.json', 'ownership.json'):
            if (path / name).exists():
                shutil.copyfile(path / name, evidence / ('0-' + name))
        manifest['staging_isolation_supported'] = d.get('staging_isolation_supported', False)
        manifest['cleanup'] = read(path / 'cleanup.json', {'state': 'unknown'})
        manifest['slot_id'] = read(path / 'ownership.json', {}).get('slot_id')
        manifest['files'] = {}
        for file in out.rglob('*'):
            if file.is_file() and not file.is_symlink() and file.name != 'remote-manifest.json':
                manifest['files'][str(file.relative_to(out))] = hashlib.sha256(file.read_bytes()).hexdigest()
        atomic(out / 'remote-manifest.json', manifest)
        with tarfile.open(path / 'result.tar.gz.tmp', 'w:gz') as archive:
            for file in out.rglob('*'):
                if file.is_file() and not file.is_symlink():
                    archive.add(file, arcname=str(file.relative_to(out)))
        os.replace(path / 'result.tar.gz.tmp', path / 'result.tar.gz')
        passed_runner = manifest.get('environment', {}).get('ready') and manifest.get('codex', {}).get('ran')
        self.set_status(path, 'done' if passed_runner else 'failed')

    def stop_children(self, unit):
        result = command(['systemctl', 'show', unit, '--property=ControlGroup', '--value'])
        group = result.stdout.strip()
        if group != '/system.slice/' + unit:
            return
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        files = list(root.rglob('cgroup.procs')) if root.exists() else []
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for file in files:
                try:
                    ids = file.read_text().split()
                except FileNotFoundError:
                    continue
                for value in ids:
                    pid = int(value)
                    if pid != os.getpid():
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
            if sig == signal.SIGTERM:
                time.sleep(1)

    def telemetry(self, path, record):
        result = command(['systemctl', 'show', record['unit'], '--property=MemoryPeak',
                          '--property=CPUUsageNSec', '--property=MemoryCurrent', '--property=ControlGroup'])
        group = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line).get('ControlGroup', '')
        events = {}
        if group == '/system.slice/' + record['unit']:
            for name in ('memory.events', 'memory.peak', 'cpu.stat'):
                file = Path('/sys/fs/cgroup') / group.lstrip('/') / name
                if file.exists(): events[name] = file.read_text()
        atomic(path / 'worker-telemetry.json', {'at': time.time(), 'unit': result.stdout,
                                             'cgroup_events': events,
                                             'disk_free_bytes': shutil.disk_usage(self.home).free})

    def cleanup(self, record, release=False):
        path = self.directory(record['attempt_id'])
        slot = record['slot_id']
        slot_record = self.slots / f'{slot}.json'
        # Caller owns slot lock; admission guard protects reservations and free transition.
        current = read(slot_record)
        if current is None or current['attempt_id'] != path.name:
            return
        if (path / 'retain').exists() and not release:
            record.update(state='retained', expires_at=time.time() + self.cfg['retention_seconds'])
            atomic(slot_record, record)
            atomic(path / 'cleanup.json', {'state': 'retained', 'expires_at': record['expires_at']})
            return
        record['state'] = 'cleaning'
        atomic(slot_record, record)
        try:
            deadline = time.monotonic() + self.cfg['cleanup_timeout']
            def cleanup_command(args, **kwargs):
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('cleanup deadline exceeded')
                return subprocess.run(args, text=True, capture_output=True, timeout=min(60, remaining), **kwargs)
            self.stop_children(record['unit'])
            cluster = 'zora-qa-' + hashlib.sha256(path.name.encode()).hexdigest()[:24]
            result = cleanup_command(['kind', 'delete', 'cluster', '--name', cluster])
            if result.returncode:
                raise RuntimeError('Kind cleanup failed: ' + result.stderr)
            result = cleanup_command(['docker', 'ps', '-aq', '--filter', 'label=io.x-k8s.kind.cluster=' + cluster])
            if result.returncode or result.stdout.strip():
                raise RuntimeError('cluster containers still exist or Docker unavailable')
            helper = path / 'in/qa-network.sh'
            if record['env']['QA_NET_ISOLATION'] == 'netns':
                result = cleanup_command(['bash', str(helper), 'down'], env=os.environ | record['env'])
                if result.returncode:
                    raise RuntimeError('network cleanup failed: ' + result.stderr)
            for name in ('work', 'scratch', 'staging'):
                target = path / name
                if target.exists():
                    if target.is_symlink():
                        target.unlink()
                    else:
                        shutil.rmtree(target)
            atomic(path / 'cleanup.json', {'state': 'clean', 'finished_at': time.time()})
            with lock(self.guard):
                slot_record.unlink(missing_ok=True)
        except Exception as exc:
            record.update(state='quarantined', reason=str(exc))
            atomic(slot_record, record)
            atomic(path / 'cleanup.json', record)
            print('quarantined: ' + str(exc), flush=True)

    def failure(self, path, d, reason):
        # Preserve existing runner evidence; otherwise create a checkable infrastructure result.
        out = path / 'out'
        out.mkdir(exist_ok=True)
        try:
            self.sanitize(path)
        except RuntimeError as exc:
            reason = str(exc)
        if not out.is_symlink():
            evidence = out / 'evidence'
            evidence.mkdir(exist_ok=True)
            atomic(evidence / '0-worker-failure.json', {'reason': reason, 'at': time.time()})
            manifest = {k: d[k] for k in ('job_id', 'attempt_id', 'worker_id', 'environment_id',
                                         'commit', 'base', 'charter_sha256', 'bundle_sha256')}
            manifest.update(protocol_version=4, runner_version=4, staging_isolation_supported=d.get('staging_isolation_supported', False),
                            environment={'ready': False, 'reason': reason}, codex={'ran': False}, files={})
            for file in out.rglob('*'):
                if file.is_file() and not file.is_symlink() and file.name != 'remote-manifest.json':
                    manifest['files'][str(file.relative_to(out))] = hashlib.sha256(file.read_bytes()).hexdigest()
            atomic(out / 'remote-manifest.json', manifest)
            import tarfile
            with tarfile.open(path / 'result.tar.gz.tmp', 'w:gz') as archive:
                for file in out.rglob('*'):
                    if file.is_file() and not file.is_symlink():
                        archive.add(file, arcname=str(file.relative_to(out)))
            os.replace(path / 'result.tar.gz.tmp', path / 'result.tar.gz')
        self.set_status(path, 'packaging' if (path / 'ownership.json').exists() else 'failed', reason)

    def reap(self, release=None):
        # Lock order: never wait for a slot while holding admission. Try slot then admission
        # only for state transitions; execute uses admission + nonblocking slot acquisition.
        for file in self.slots.glob('*.json'):
            with lock(file.with_suffix('.lock'), False) as acquired:
                if acquired is None:
                    continue
                record = read(file)
                if not record or active(record['unit']):
                    continue
                path = self.directory(record['attempt_id'])
                expired = record.get('expires_at', float('inf')) <= time.time()
                if record['state'] == 'retained' and not expired and release != path.name:
                    continue
                if self.status(path.name) not in TERMINAL:
                    self.failure(path, self.dispatch(path), 'worker unit stopped before completion')
                self.cleanup(record, release=expired or release == path.name)
                self.package(path, self.dispatch(path))
        for path in self.jobs.iterdir():
            if not path.is_dir() or not (path / 'worker-state.json').exists():
                continue
            with lock(self.guard):
                if self.status(path.name) not in TERMINAL and not active(unit_name(path.name)):
                    self.failure(path, self.dispatch(path), 'supervisor lost; attempt must be retried explicitly')

    def cancel(self, attempt):
        path = self.directory(attempt)
        d = self.dispatch(path)
        if self.status(attempt) in TERMINAL:
            return self.status(attempt)
        with lock(self.guard):
            self.set_status(path, 'cancelling')
        command(['systemctl', 'stop', unit_name(attempt)])
        self.failure(path, d, 'cancelled by dispatcher')
        self.reap()
        self.set_status(path, 'cancelled')
        return 'cancelled'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True)
    parser.add_argument('action', choices=['submit', 'status', 'cancel', 'execute', 'reap', 'release', 'check'])
    parser.add_argument('target', nargs='?')
    args = parser.parse_args()
    worker = Worker(args.home)
    if args.action in {'submit', 'status', 'cancel', 'execute', 'release'} and not args.target:
        parser.error('target required')
    if args.action == 'submit':
        print(worker.submit(args.target))
    elif args.action == 'status':
        print(worker.status(args.target))
    elif args.action == 'cancel':
        print(worker.cancel(args.target))
    elif args.action == 'execute':
        worker.execute(args.target)
    elif args.action == 'reap':
        worker.reap()
    elif args.action == 'release':
        worker.reap(release=args.target)
    else:
        print(json.dumps({'protocol_version': PROTOCOL, 'worker_id': worker.cfg['worker_id'],
                          'slots': worker.cfg['slots'], 'isolation': worker.cfg['isolation']}))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('QA worker: ' + str(exc), file=sys.stderr)
        sys.exit(1)
