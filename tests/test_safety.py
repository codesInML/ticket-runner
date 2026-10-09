"""Regression checks for process lifetime and cross-repository dependency isolation."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ticket_runner_lib.github import native_links
from ticket_runner_lib.processes import kill, probe
from ticket_runner_lib.verification import Verification
from types import SimpleNamespace


class SafetyTests(unittest.TestCase):
    def test_nested_checkpoints_and_verification_keep_items_but_skip_code(self):
        verification = Verification()
        verification.a = SimpleNamespace(parent=207)
        verification.order = [209]
        verification.tickets = {209: {'body': '## Verification\n### Manual\n- Check endpoint\n'
                                     '```sh\n- this is code\n```\n## Other\n- not a check'}}
        verification.parent_issue = lambda: {'body': '## Checkpoint: app\n### Manual checks\n'
                                            '- App answers\n## Other\n- excluded'}
        checks = verification.check_manifest()
        self.assertEqual([check['text'] for check in checks], ['Check endpoint', 'App answers'])
        self.assertEqual([check['id'] for check in checks], ['C001', 'C002'])

    def test_foreign_child_cannot_override_local_dependency_edges(self):
        def links(n):
            return {'totalCount': 1, 'nodes': [{'number': n, 'state': 'OPEN',
                                               'repository': {'nameWithOwner': 'acme/home'}}]}
        child = {'number': 209, 'repository': {'nameWithOwner': 'foreign/repo'}, 'blockedBy': links(88)}
        response = {'data': {'repository': {'nameWithOwner': 'acme/home',
                    'parent': {'subIssues': {'totalCount': 1, 'nodes': [child]}},
                    'i209': {'blockedBy': links(77)}}}}
        with patch('ticket_runner_lib.github.gh_json', return_value=response):
            _, _, edges = native_links(207, [209])
        self.assertEqual(edges[209]['nodes'][0]['number'], 77)

    def test_readiness_timeout_kills_the_entire_probe_group(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'descendant.pid'
            started = time.monotonic()
            self.assertFalse(probe('sleep 60 & echo $! > descendant.pid; wait', folder, os.environ, timeout=.2))
            self.assertLess(time.monotonic() - started, 2)
            pid = int(marker.read_text())
            self.assert_process_dead(pid)

    def test_cleanup_kills_descendants_when_shell_already_exited(self):
        with tempfile.TemporaryDirectory() as folder:
            source = ("import os,signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      "Path('descendant.pid').write_text(str(os.getpid())); time.sleep(60)")
            import shlex
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)} & " \
                "while [ ! -f descendant.pid ]; do sleep .01; done"
            child = subprocess.Popen(command, shell=True, cwd=folder, start_new_session=True)
            child.wait()
            kill(child)
            self.assert_process_dead(int((Path(folder) / 'descendant.pid').read_text()))

    def assert_process_dead(self, pid):
        for _ in range(50):
            result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                return
            time.sleep(.02)
        os.kill(pid, signal.SIGKILL)
        self.fail(f'process {pid} survived cleanup')
