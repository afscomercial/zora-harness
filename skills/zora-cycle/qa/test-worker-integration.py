#!/usr/bin/env python3
"""Opt-in root/Linux systemd integration. Synthetic runners only: no app QA."""
import argparse, hashlib, json, os, shutil, subprocess, tarfile, tempfile, time, uuid
from pathlib import Path

RUNNER = '''#!/usr/bin/env bash
set -eu
python3 - "$(cd "$(dirname "$0")/.." && pwd)" <<'FIXTURE'
import json,os,pathlib,subprocess,sys,tarfile,time
p=pathlib.Path(sys.argv[1])
with (p/'launches').open('a') as f: f.write(str(os.getpid())+'\\n')
child=subprocess.Popen(['sleep','300'])
(p/'child.pid').write_text(str(child.pid))
while not (p/'finish').exists(): time.sleep(.05)
child.terminate(); child.wait()
out=p/'out'; out.mkdir(exist_ok=True)
(out/'evidence').mkdir(exist_ok=True)
(out/'evidence/synthetic.txt').write_text('Supervisor fixture only; no application QA.\\n')
m=json.loads((p/'in/dispatch.json').read_text())
m.update(runner_version=4,environment={'ready':True},codex={'ran':True,'exit_code':0},files={})
(out/'remote-manifest.json').write_text(json.dumps(m))
with tarfile.open(p/'result.tar.gz','w:gz') as t:
 for f in out.rglob('*'):
  if f.is_file(): t.add(f,arcname=str(f.relative_to(out)))
(p/'runner-status').write_text('done\\n')
FIXTURE
'''

def run(args):
    p=subprocess.run(args,text=True,capture_output=True,timeout=40)
    assert p.returncode == 0, (args,p.stdout,p.stderr)
    return p.stdout.strip()

def wait(check,label):
    end=time.monotonic()+30
    while time.monotonic()<end:
        if check(): return
        time.sleep(.15)
    raise AssertionError('timeout: '+label)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',required=True)
    args=parser.parse_args()
    assert os.geteuid()==0 and Path('/run/systemd/system').exists()
    home=Path(tempfile.mkdtemp(prefix='qa-worker-integration-',dir='/tmp'))
    worker=home/'qa-worker.py'; shutil.copyfile(args.worker,worker)
    (home/'worker.json').write_text(json.dumps(dict(worker_id='integration',slots=1,isolation='none',driver='kind',poll_seconds=.1,queue_timeout=90,run_timeout=120,cleanup_timeout=20,min_disk_gb=1,host_reserve_mb=128,slot_memory_mb=256,process_memory_max_mb=128,cluster_memory_max_mb=128,max_load_per_cpu=100)))
    attempts=[]
    def cmd(action,target=None):
        return run(['python3',str(worker),'--home',str(home),action]+([str(target)] if target else []))
    def new():
        ident='it-'+uuid.uuid4().hex; p=home/'jobs'/ident; bundle=p/'in'; bundle.mkdir(parents=True)
        files={'qa-job.sh':RUNNER.encode(),'qa-charter.md':b'Synthetic supervisor integration only.\n'}
        for n,b in files.items(): (bundle/n).write_bytes(b)
        digest=hashlib.sha256(''.join(n+'\0'+hashlib.sha256(b).hexdigest()+'\n' for n,b in sorted(files.items())).encode()).hexdigest()
        d=dict(protocol_version=4,worker_id='integration',job_id=ident,attempt_id=ident,environment_id=ident,base='a'*40,commit='b'*40,repo='https://example.invalid/repo',profile='infra',services=[],charter_sha256=hashlib.sha256(files['qa-charter.md']).hexdigest(),bundle_sha256=digest)
        (bundle/'dispatch.json').write_text(json.dumps(d)); attempts.append(p); return p
    def finish(p):
        (p/'finish').touch(); wait(lambda:cmd('status',p.name)=='done','terminal archive')
        with tarfile.open(p/'result.tar.gz') as t:
            m=json.load(t.extractfile('remote-manifest.json'))
            assert m['cleanup']['state']=='clean',m
            assert 'evidence/0-cleanup.json' in m['files']
    try:
        a,b,c=new(),new(),new(); cmd('submit',a)
        wait(lambda:(a/'launches').exists(),'first runner'); cmd('submit',a)
        assert len((a/'launches').read_text().splitlines())==1
        cmd('submit',b); cmd('submit',c)
        assert cmd('status',b.name)=='queued' and not (b/'launches').exists()
        assert cmd('cancel',c.name)=='cancelled' and (c/'result.tar.gz').exists() and not (c/'launches').exists()
        finish(a); wait(lambda:(b/'launches').exists(),'queued admission'); finish(b)
        print('PASS idempotence, same-slot queue, queued cancellation, terminal archive',flush=True)
        d=new(); cmd('submit',d); wait(lambda:(d/'child.pid').exists(),'crash fixture')
        child=int((d/'child.pid').read_text()); unit='zora-qa-'+d.name+'.service'
        run(['systemctl','kill','--kill-whom=main','--signal=SIGKILL',unit])
        def inactive():
            p=subprocess.run(['systemctl','is-active',unit],text=True,capture_output=True)
            return p.stdout.strip() not in ('active','activating','deactivating')
        wait(inactive,'unit inactive'); cmd('reap')
        assert cmd('status',d.name)=='failed' and (d/'result.tar.gz').exists()
        assert json.loads((d/'cleanup.json').read_text())['state']=='clean'
        assert not list((home/'slots').glob('*.json'))
        stat=Path(f'/proc/{child}/stat')
        assert not stat.exists() or stat.read_text().split()[2]=='Z','descendant survived'
        print('PASS parent SIGKILL, descendant termination, reaper cleanup, failed archive, slot release',flush=True)
        print('Synthetic supervisor tests passed; no application QA performed.',flush=True)
    finally:
        for p in attempts: subprocess.run(['systemctl','stop','zora-qa-'+p.name+'.service'],capture_output=True,timeout=40)
        try: cmd('reap')
        except Exception as exc: print('Cleanup review required:',exc)
        if list((home/'slots').glob('*.json')): print('Retained test home:',home)
        else: shutil.rmtree(home)

if __name__=='__main__': main()
