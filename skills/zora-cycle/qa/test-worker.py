#!/usr/bin/env python3
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('worker', Path(__file__).with_name('qa-worker.py'))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.worker = w.Worker(self.home)
        self.path = self.worker.directory('attempt-a')
        (self.path / 'in').mkdir(parents=True)
        (self.path / 'in/qa-charter.md').write_text('acceptance criteria')
        (self.path / 'in/qa-job.sh').write_text('exit 0')
        digest = hashlib.sha256()
        for p in sorted((self.path / 'in').iterdir()):
            digest.update((p.name + '\0' + hashlib.sha256(p.read_bytes()).hexdigest() + '\n').encode())
        self.d = dict(protocol_version=4, job_id='job-a', attempt_id='attempt-a', environment_id='attempt-a',
                      worker_id='vps-1', base='a'*40, commit='b'*40, repo='git@example.test:repo',
                      profile='infrastructure', services=[], bundle_sha256=digest.hexdigest(),
                      charter_sha256=hashlib.sha256((self.path/'in/qa-charter.md').read_bytes()).hexdigest())
        w.atomic(self.path/'in/dispatch.json',self.d)
    def tearDown(self):
        self.tmp.cleanup()
    def test_failure_after_slot_release_is_terminal(self):
        w.atomic(self.path/'ownership.json', {'slot_id': 1, 'attempt_id': self.path.name})
        self.worker.failure(self.path,self.d,'interrupted after cleanup')
        self.assertEqual(self.worker.status(self.path.name),'failed')
    def test_redaction_receives_vm_and_slot_environment(self):
        (self.home/'vm.env').write_text('WORKER_TEST_SECRET=private-value\nQA_AUTH_FILE=/wrong/auth\n')
        w.atomic(self.path/'ownership.json', {'env': {'QA_AUTH_FILE': '/slot/auth'}})
        (self.path/'out').mkdir()
        (self.path/'in/redact-evidence.py').write_text('import os; assert os.environ["WORKER_TEST_SECRET"] == "private-value"; assert os.environ["QA_AUTH_FILE"] == "/slot/auth"')
        self.worker.sanitize(self.path)
        self.assertFalse(list(self.path.glob('unredacted-out-*')))

    def test_under_reserved_profile_rejected(self):
        w.atomic(self.home/'worker.json', {'profiles': {'heavy': 100}})
        with self.assertRaisesRegex(ValueError, 'reservation'): w.Worker(self.home)
    def test_exclusive_legacy_reservation_blocks_new_jobs(self):
        w.atomic(self.worker.slots/'1.json', {'memory_mb': 100, 'exclusive': True})
        with patch.object(self.worker,'memory',return_value={'MemTotal':64000,'MemAvailable':63000}), patch.object(w.os,'getloadavg',return_value=(0,0,0)):
            self.assertIn('exclusive legacy environment', self.worker.admission(13000))
    def test_failed_redaction_never_exports_raw_evidence(self):
        out=self.path/'out'; (out/'evidence').mkdir(parents=True)
        (out/'evidence/raw.log').write_text('secret-value')
        (self.path/'in/redact-evidence.py').write_text('raise SystemExit(1)')
        self.worker.failure(self.path,self.d,'runner failed')
        import tarfile
        with tarfile.open(self.path/'result.tar.gz') as archive:
            self.assertNotIn('evidence/raw.log', archive.getnames())
        self.assertEqual(len(list(self.path.glob('unredacted-out-*'))),1)

    def test_multiple_slots_require_network_isolation(self):
        w.atomic(self.home/'worker.json',{'slots':2})
        with self.assertRaises(ValueError): w.Worker(self.home)
    def test_bundle_tampering_rejected(self):
        (self.path/'in/qa-job.sh').write_text('different')
        with self.assertRaisesRegex(ValueError,'bundle hash'): self.worker.dispatch(self.path)
    def test_dispatch_worker_mismatch(self):
        self.d['worker_id']='other'; w.atomic(self.path/'in/dispatch.json',self.d)
        with self.assertRaisesRegex(ValueError,'mismatch'): self.worker.dispatch(self.path)
    def test_directory_escape_rejected(self):
        with self.assertRaises(ValueError): self.worker.directory('../other')
    def test_double_submit_starts_one_unit(self):
        result=subprocess.CompletedProcess([],0,'','')
        with patch.object(w,'command',return_value=result) as command, patch.object(w,'active',return_value=True):
            self.assertEqual(self.worker.submit(self.path),'queued')
            self.assertEqual(self.worker.submit(self.path),'queued')
            self.assertEqual(command.call_count,1)
    def test_reservations_prevent_double_spend(self):
        w.atomic(self.worker.slots/'1.json',{'memory_mb':20000})
        with patch.object(self.worker,'memory',return_value={'MemTotal':32000,'MemAvailable':31000}), patch.object(w.os,'getloadavg',return_value=(0,0,0)):
            self.assertIn('reserved memory',self.worker.admission(13000))
    def test_nonblocking_lock_does_not_reclaim_owner(self):
        with w.lock(self.home/'held') as owner:
            with w.lock(self.home/'held',False) as other:
                self.assertIsNotNone(owner); self.assertIsNone(other)
    def test_failure_packages_identity_and_reason(self):
        self.worker.failure(self.path,self.d,'capacity timeout')
        m=w.read(self.path/'out/remote-manifest.json')
        self.assertEqual(m['attempt_id'],'attempt-a')
        self.assertFalse(m['codex']['ran'])
        self.assertTrue((self.path/'result.tar.gz').exists())
        self.assertEqual(self.worker.status('attempt-a'),'failed')
    def test_shared_identity_requires_explicit_policy(self):
        self.worker.cfg.update(slots=2,identity_bundles={'1':{'auth_file':'/auth'},'2':{'auth_file':'/auth'}})
        w.atomic(self.path/'worker-state.json',{'submitted_at':0})
        with self.assertRaisesRegex(ValueError,'shared concurrent'): self.worker.environment(self.path,1,self.d)
        self.worker.cfg['shared_identity_concurrency_verified']=True
        self.assertEqual(self.worker.environment(self.path,1,self.d)['QA_AUTH_FILE'],'/auth')
    def test_cleanup_failure_quarantines_slot(self):
        record={'attempt_id':'attempt-a','slot_id':1,'unit':'unit','env':{'QA_NET_ISOLATION':'none'},'memory_mb':13000}
        w.atomic(self.worker.slots/'1.json',record)
        result=subprocess.CompletedProcess([],1,'','cannot delete')
        with patch.object(self.worker,'stop_children'),patch.object(w.subprocess,'run',return_value=result):
            self.worker.cleanup(record)
        self.assertEqual(w.read(self.worker.slots/'1.json')['state'],'quarantined')
    def test_retained_environment_keeps_reservation(self):
        record={'attempt_id':'attempt-a','slot_id':1,'unit':'unit','memory_mb':13000}
        w.atomic(self.worker.slots/'1.json',record)
        (self.path/'retain').touch()
        self.worker.cleanup(record)
        self.assertEqual(w.read(self.worker.slots/'1.json')['state'],'retained')
    def test_reaper_never_cleans_live_unit(self):
        record={'attempt_id':'attempt-a','slot_id':1,'unit':'unit','state':'reserved'}
        w.atomic(self.worker.slots/'1.json',record)
        with patch.object(w,'active',return_value=True),patch.object(self.worker,'cleanup') as cleanup:
            self.worker.reap()
            cleanup.assert_not_called()
    def test_package_binds_cleanup_and_worker_metrics(self):
        self.worker.failure(self.path,self.d,'test')
        w.atomic(self.path/'cleanup.json',{'state':'clean'})
        w.atomic(self.path/'worker-telemetry.json',{'peak':42})
        self.worker.package(self.path,self.d)
        m=w.read(self.path/'out/remote-manifest.json')
        self.assertEqual(m['cleanup']['state'],'clean')
        self.assertIn('evidence/0-worker-telemetry.json',m['files'])

if __name__=='__main__': unittest.main()
