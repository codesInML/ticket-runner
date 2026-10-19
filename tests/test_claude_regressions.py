"""Offline regression checks for backend isolation, cancellation and durable costs."""
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ticket_runner_lib.agents import AgentProcesses
from ticket_runner_lib.backends import ClaudeCode, ClaudeStream, company_key
from ticket_runner_lib.settings import Fatal, Stop


class Harness(AgentProcesses):
    def __init__(self):
        self.data = {'_meta': {}}
        self.state = threading.RLock()
        self.spawn = threading.Lock()
        self.stopping = threading.Event()
        self.children = set()
        self.fatal = None

    def persist(self):
        pass

    def update(self, record, **fields):
        record.update(fields)


class ClaudeRegressions(unittest.TestCase):
    def test_project_settings_cannot_supply_authentication(self):
        argv = ClaudeCode('fake', Path('/config')).argv('sonnet', None, 1, 'hello')
        self.assertIn('--setting-sources', argv)
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'user')

    def test_all_provider_selectors_are_removed(self):
        selectors = {f'CLAUDE_CODE_USE_{name}': '1' for name in
                     ('ANTHROPIC_AWS', 'ANTHROPIC_GOOGLE_CLOUD', 'MANTLE', 'GATEWAY', 'FUTURE_PROVIDER')}
        with patch.dict(os.environ, selectors):
            env = ClaudeCode('fake', Path('/config')).env()
        self.assertFalse(set(selectors) & set(env))

    def test_authentication_headers_and_token_handoffs_are_removed(self):
        leaked = {name: 'fake' for name in ('ANTHROPIC_CUSTOM_HEADERS', 'ANTHROPIC_UNIX_SOCKET',
                  'CCR_OAUTH_TOKEN_FILE', 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR',
                  'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR', 'CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR')}
        with patch.dict(os.environ, leaked):
            env = ClaudeCode('company', Path('/config')).env()
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual(env['ANTHROPIC_API_KEY'], 'company')

    def test_init_refuses_other_providers_even_with_the_company_key_source(self):
        for provider in ('anthropicAws', 'vertex', 'gateway'):
            stream = ClaudeStream()
            stream.feed({'type': 'system', 'subtype': 'init', 'apiKeySource': 'ANTHROPIC_API_KEY',
                         'apiProvider': provider})
            self.assertIsNotNone(stream.refusal)
        stream = ClaudeStream()
        stream.feed({'type': 'system', 'subtype': 'init', 'apiKeySource': 'ANTHROPIC_API_KEY',
                     'apiProvider': 'firstParty'})
        self.assertIsNone(stream.refusal)

    def test_company_key_capture_removes_the_override(self):
        with patch.dict(os.environ, {'TICKET_RUNNER_ANTHROPIC_API_KEY': ' fake '}):
            self.assertEqual(company_key(), ('fake', 'env'))
            self.assertNotIn('TICKET_RUNNER_ANTHROPIC_API_KEY', os.environ)

    def test_fatal_parallel_job_cancels_sibling_child_and_preserves_failure(self):
        runner = Harness()
        started = threading.Event()
        def sibling():
            child = runner.spawn_child(['python3', '-c', 'import time; time.sleep(10)'])
            started.set()
            try:
                child.wait()
                if runner.stopping.is_set():
                    raise Stop()
            finally:
                runner.reap(child)
        failure = Fatal('company billing failure')
        def failing():
            self.assertTrue(started.wait(3))
            raise failure
        began = time.monotonic()
        with self.assertRaises(Fatal) as caught:
            runner.parallel([sibling, failing])
        self.assertIs(caught.exception, failure)
        self.assertLess(time.monotonic() - began, 5)

    def test_costs_survive_stage_replacement_without_double_counting_resume(self):
        runner = Harness()
        old = {'sessionId': 'review'}
        runner.data['_review'] = old
        runner.spend(old, 'epic review', 'opus', 'high', {'costUsd': 5}, None, 'reported', 'review')
        runner.data['_review'] = {'sessionId': 'review2'}
        runner.spend(runner.data['_review'], 'epic review', 'opus', 'high', {'costUsd': 2.5}, None, 'reported', 'review')
        self.assertEqual(runner.spent_total(), 7.5)
        runner.spend(runner.data['_review'], 'epic review', 'opus', 'high', {'costUsd': 3}, 'review2', 'reported', 'review')
        self.assertEqual(runner.spent_total(), 8)

    def test_cost_queries_do_not_write_state(self):
        runner = Harness()
        runner.data['209'] = {'costs': [{'stage': 'implement', 'costUsd': 1.25}]}
        with patch.object(runner, 'persist') as persist:
            self.assertEqual(runner.spent_total(), 1.25)
            persist.assert_not_called()

    def test_legacy_costs_migrate_once_with_ticket_ownership(self):
        runner = Harness()
        runner.data['209'] = {'costs': [{'stage': 'implement', 'costUsd': 1.25}]}
        self.assertEqual(runner.costs(), [('#209', {'stage': 'implement', 'costUsd': 1.25})])
        runner.data.pop('209')
        self.assertEqual(runner.spent_total(), 1.25)
