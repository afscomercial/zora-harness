#!/usr/bin/env python3
"""Evidence may contain source text, but never links, escapes, or executable modes."""
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

DISPATCHER=Path(__file__).with_name('run-codex-qa')

class ExtractionTests(unittest.TestCase):
    def extract(self, entries):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); archive=root/'bundle.tar.gz'; out=root/'out'
        with tarfile.open(archive,'w:gz') as tar:
            for name, kind in entries:
                info=tarfile.TarInfo(name); info.mode=0o777
                if kind == 'link':
                    info.type=tarfile.SYMTYPE; info.linkname='/etc/passwd'; tar.addfile(info)
                else:
                    data=b'plain evidence'; info.size=len(data); tar.addfile(info,io.BytesIO(data))
        r=subprocess.run(['bash',str(DISPATCHER),'--extract',str(archive),str(out)],capture_output=True,text=True)
        return r,out
    def test_browser_text_is_nonexecutable(self):
        r,out=self.extract([('evidence/page.html','file'),('evidence/replay.cjs','file'),('evidence/1-diff.patch','file')])
        self.assertEqual(r.returncode,0,r.stderr)
        for p in out.rglob('*'):
            if p.is_file(): self.assertEqual(p.stat().st_mode & 0o777,0o644)
    def test_link_rejects_whole_bundle(self):
        r,out=self.extract([('evidence/good.txt','file'),('evidence/bad.html','link')])
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((out/'evidence/good.txt').exists())
    def test_traversal_rejected(self):
        r,out=self.extract([('../escape.txt','file')])
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((out.parent/'escape.txt').exists())

if __name__=='__main__': unittest.main()
