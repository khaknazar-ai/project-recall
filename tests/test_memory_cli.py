"""Two small public-CLI checks using only fresh, synthetic Windows projects."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path('C:/AI/temp/questmemory-recovery')

@unittest.skipUnless(os.name == 'nt' and SCRATCH.is_dir(), 'Requires the documented Windows recovery namespace')
class MemoryCliTests(unittest.TestCase):
    def setUp(self):
        self.base = SCRATCH / ('project-recall-smoke-' + uuid.uuid4().hex)
        self.base.mkdir()
        self.root, self.target = self.base / 'controller', self.base / 'target'
        self.root.mkdir(); self.target.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k.upper() in {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'}}
        self.env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', PYTHONUTF8='1', TEMP=str(self.base), TMP=str(self.base))
        self.cli('create', '--target-root', self.target, '--id', 'memory-demo', '--objective', 'Remember reviewed project decisions.')

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-I', '-B', '-S', '-X', 'utf8', str(ROOT / 'project_recall.py'),
                                 *map(str, args), '--project-root', str(self.root)],
                                cwd=self.base, env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        if expected == 0:
            self.assertEqual(result.stderr, '')
        return result.stdout

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def proposal(self):
        report = self.root / '.questmemory/SESSION_REPORTS/demo.md'
        report.parent.mkdir(exist_ok=True)
        report.write_text('# Demo\n\n## Decisions Proposed\n- storage: Keep reviewed decisions local.\n', encoding='utf-8')
        return json.loads(self.cli('propose', report))[0]

    def approval(self, row):
        text = 'Approve memory ' + row['patch_sha256'] + ' ' + row['proposal_sha256']
        return ['approve', row['patch_id'], '--sha256', row['patch_sha256'], '--approval-id', 'demo-approval',
                '--approval-statement', text, '--approval-text-sha256', hashlib.sha256(text.encode()).hexdigest(),
                '--confirm-human-approval']

    def test_approval_cold_restore_and_replay(self):
        row = self.proposal()
        self.assertEqual(json.loads(self.cli('show', row['patch_id']))['memory']['statement'], 'Keep reviewed decisions local.')
        approval = self.approval(row)
        pending = self.snapshot()
        self.cli(*approval[:-1], expected=1)
        self.assertEqual(self.snapshot(), pending)
        wrong = list(approval)
        wrong[wrong.index('--approval-statement') + 1] += ' changed'
        self.cli(*wrong, expected=1)
        self.assertEqual(self.snapshot(), pending)
        self.cli(*approval)
        approved = self.snapshot()
        context = json.loads(self.cli('resume'))
        self.assertEqual(context['memory'][0]['statement'], 'Keep reviewed decisions local.')
        self.assertFalse(context['authority_granted'])
        self.assertFalse(context['replay_allowed'])
        self.assertEqual(json.loads(self.cli('status')), context)
        self.assertTrue(json.loads(self.cli(*approval))['replay_noop'])
        self.assertEqual(self.snapshot(), approved)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_owner_and_path_isolation(self):
        self.assertEqual(json.loads(self.cli('memory', '--json'))['memory'], [])
        before = self.snapshot()
        self.cli('create', '--target-root', self.root, '--id', 'memory-demo', '--objective', 'Refuse overlap.', expected=1)
        self.assertEqual(self.snapshot(), before)
        config = self.root / 'questmemory.toml'
        text, count = re.subn(r'(sqlite_owner_token\s*=\s*")[a-f0-9]{64}("\s*)',
                             r'\g<1>' + '0' * 64 + r'\2', config.read_text(encoding='utf-8'))
        self.assertEqual(count, 1)
        config.write_text(text, encoding='utf-8')  # Negative fixture only; never successful setup.
        invalid = self.snapshot()
        self.cli('status', expected=1)
        self.assertEqual(self.snapshot(), invalid)

if __name__ == '__main__':
    unittest.main()