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

from ticket_runner_lib.github import missing_labels, native_links, transient
from ticket_runner_lib.processes import kill, probe, remote
from ticket_runner_lib.verification import Verification
from types import SimpleNamespace


class SafetyTests(unittest.TestCase):
    def test_a_label_github_cannot_find_is_named_in_any_of_its_wordings(self):
        self.assertEqual(missing_labels("failed to update https://github.com/o/r/issues/1: 'needs-human' not found"),
                         ['needs-human'])
        self.assertEqual(missing_labels('labels not found: in-progress, agent-done'), ['in-progress', 'agent-done'])
        self.assertEqual(missing_labels('HTTP 404: Not Found'), [])

    def test_only_github_being_out_of_reach_is_retried(self):
        for said in ('Post "https://api.github.com/graphql": dial tcp 140.82.121.5:443: i/o timeout',
                     'HTTP 502: Bad Gateway', 'API rate limit exceeded', 'error connecting to api.github.com', ''):
            self.assertTrue(transient(said), said)
        for said in ('HTTP 403: Resource not accessible by integration', 'Could not resolve to an issue',
                     "'needs-human' not found"):
            self.assertFalse(transient(said), said)

    def test_remote_git_retries_only_while_the_network_is_down(self):
        offline = subprocess.CompletedProcess([], 128, "", "fatal: unable to access 'https://github.com/o/r.git/': "
                                              "Failed to connect to github.com port 443: Couldn't connect to server")
        online = subprocess.CompletedProcess([], 0, "abc\trefs/heads/x\n", "")
        missing = subprocess.CompletedProcess([], 2, "", "")
        with patch("ticket_runner_lib.processes.subprocess.run", side_effect=[offline, offline, online]) as run, \
                patch("ticket_runner_lib.processes.time.sleep") as sleep:
            self.assertIs(remote("/repo", "ls-remote", "origin"), online)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])
        with patch("ticket_runner_lib.processes.subprocess.run", side_effect=[missing]) as run, \
                patch("ticket_runner_lib.processes.time.sleep") as sleep:
            self.assertIs(remote("/repo", "ls-remote", "--exit-code", "origin"), missing)
        sleep.assert_not_called()
        with patch("ticket_runner_lib.processes.subprocess.run", return_value=offline) as run, \
                patch("ticket_runner_lib.processes.time.sleep"):
            self.assertIs(remote("/repo", "push", "origin"), offline)
        self.assertEqual(run.call_count, 4)

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
