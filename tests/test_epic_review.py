"""Focused checks for report parsing and review input boundaries."""
from types import SimpleNamespace
import unittest
from unittest import mock

from ticket_runner_lib.epic_review import EpicReview
from ticket_runner_lib.text import review_rows


class FixTicketFilingTests(unittest.TestCase):
    MARKER = '<!-- ticket-runner: epic #628 review a00a1e4a9268 draft 01 -->'

    def file(self, listings, creates):
        review = EpicReview()
        review.link_fix_ticket = mock.Mock()
        runs = [SimpleNamespace(returncode=code, stdout=out, stderr=err) for code, out, err in creates]
        with mock.patch('ticket_runner_lib.epic_review.gh_json', side_effect=listings), \
                mock.patch('ticket_runner_lib.epic_review.subprocess.run', side_effect=runs) as run, \
                mock.patch('ticket_runner_lib.epic_review.time.sleep'):
            result = review.create_fix_ticket('Fix', 'draft.md', self.MARKER)
        return result, run.call_count, review.link_fix_ticket

    def test_a_dropped_connection_is_retried(self):
        timeout = (1, '', 'Post "https://api.github.com/graphql": dial tcp 140.82.121.5:443: i/o timeout')
        result, creates, link = self.file([[], []], [timeout, (0, 'https://github.com/o/r/issues/700\n', '')])
        self.assertEqual(result, (700, None))
        self.assertEqual(creates, 2)
        link.assert_called_once_with(700)

    def test_a_create_github_accepted_is_found_not_filed_twice(self):
        timeout = (1, '', 'i/o timeout')
        result, creates, _ = self.file([[], [{'number': 700, 'body': f'Fix\n{self.MARKER}\n'}]], [timeout])
        self.assertEqual(result, (700, None))
        self.assertEqual(creates, 1)

    def test_a_connection_that_stays_down_is_reported(self):
        result, creates, link = self.file([[]] * 4, [(1, '', 'i/o timeout')] * 4)
        self.assertEqual(result, (None, 'i/o timeout'))
        self.assertEqual(creates, 4)
        link.assert_not_called()


class ReviewInputTests(unittest.TestCase):
    def test_filing_own_fix_ticket_does_not_invalidate_in_progress_review(self):
        review = EpicReview()
        review.a = SimpleNamespace(parent=207)
        review.data = {'_meta': {'epicBase': 'base'}}
        review.parent_issue = lambda: {'title': 'Epic', 'body': 'Requirements'}
        review.tickets = {1: {'title': 'Ticket', 'body': 'Requirement', 'blockers': []}}
        review.order = [1]
        state = {'inputTickets': [1], 'filed': {}, 'start': 'abcdef123456789'}
        original = review.review_fingerprint(state)
        # Filing persisted before the process stopped, but phase is still `file`.
        review.tickets[400] = {'title': 'Fix', 'body': '<!-- ticket-runner: epic #207 review abcdef123456 draft 01 -->\nFix requirement', 'blockers': []}
        review.order.append(400)
        self.assertEqual(review.review_fingerprint(state), original)
        state['filed']['01'] = 400
        self.assertEqual(review.review_fingerprint(state), original)
        state['inputTickets'].append(400)
        self.assertNotEqual(review.review_fingerprint(state), original)
        with_fix = review.review_fingerprint(state)
        review.tickets[400]['body'] = 'Changed fix requirement'
        self.assertNotEqual(review.review_fingerprint(state), with_fix)
        before_base = review.review_fingerprint(state)
        review.data['_meta']['epicBase'] = 'different-base'
        self.assertNotEqual(review.review_fingerprint(state), before_base)

    def test_parser_keeps_indented_evidence_and_rejects_mixed_entries(self):
        entries, problem = review_rows('- D1 CONFIRMED path:1\n  quoted code and command output', ('CONFIRMED',))
        self.assertIsNone(problem)
        self.assertIn('quoted code', entries[0][2])
        for report in ('- D1 CONFIRMED path:1\n- `D2` CONFIRMED path:2', '- D1 CONFIRMED', '- D1 CONFIRMED —'):
            with self.subTest(report=report):
                self.assertIsNotNone(review_rows(report, ('CONFIRMED',))[1])

    def test_parser_skips_closing_prose_but_not_malformed_entries(self):
        report = ('- D1 DEFERRED a.ts:1 — wrong — evidence\n\n'
                  'Per instructions, formatting-only deviations are not findings. No BLOCKING defects found.')
        entries, problem = review_rows(report, ('BLOCKING', 'DEFERRED'))
        self.assertIsNone(problem)
        self.assertEqual([('D1', 'DEFERRED')], [(i, c) for i, c, _ in entries])
        self.assertNotIn('Per instructions', entries[0][2])
        for line in ('- D2 BLOKING a.ts:2 — typo', 'D2: a.ts:2 is broken', '1. a.ts:2 is broken',
                     '**D2** BLOKING a.ts:2 — typo', '- a.ts:2 is broken', '* a.ts:2 is broken',
                     '> - a.ts:2 is broken', '`D2` BLOKING a.ts:2'):
            with self.subTest(line=line):
                self.assertIsNotNone(review_rows(report + '\n' + line, ('BLOCKING', 'DEFERRED'))[1])

    def test_findings_of_none_may_give_their_reason(self):
        def parse(body):
            return EpicReview().parse_findings(f'## FINDINGS\n\n{body}\n\nFOLLOW-UP=DONE', {'letter': 'G'},
                                               r'FOLLOW-UP=(DONE)')
        for body in ('None.', 'None. The only semantic change the fix diff makes is owned by fix ticket #51.',
                     '**None** — the fixes stay within their tickets.', 'None\nThe fix diff touches three files.'):
            with self.subTest(body=body):
                self.assertEqual(parse(body), ({}, None))
        for body in ('None of the fixes hold: runner.ts:88 still drops the timer.', 'The fix diff looks fine.'):
            with self.subTest(body=body):
                self.assertIn('shape', parse(body)[1])
        found, problem = parse('None. Except:\n- G3 BLOCKING runner.ts:88 — drops the timer — evidence')
        self.assertIsNone(problem)
        self.assertEqual(['G3'], list(found))

    def test_repair_verdict_cannot_hide_blocking_findings(self):
        _, problem = EpicReview().parse_findings(
            '## FINDINGS\n- R1 BLOCKING fixed.py:1 — wrong — code evidence\nVERDICT=PASS',
            {'letter': 'R'}, r'VERDICT=(PASS|FAIL)')
        self.assertIn('contradictory', problem)


class RepairResumeTests(unittest.TestCase):
    def review(self, phase):
        review = EpicReview()
        review.a = SimpleNamespace(workflow='pocock', epic_review_models=['glm'])
        review.root = '/repo'
        review.review_fingerprint = lambda rv=None: 'same'
        rv = {'protocol': 3, 'workflow': 'pocock', 'models': ['glm'], 'fingerprint': 'same', 'phase': phase,
              'start': 'start', 'fixHead': 'fixhead'}
        return review, rv

    def resumable(self, phase, ancestor, **changes):
        review, rv = self.review(phase)
        rv.update(changes)
        merge_base = SimpleNamespace(returncode=0 if ancestor else 1)
        with mock.patch('ticket_runner_lib.epic_review.REVIEW_PROTOCOL', 3), \
                mock.patch('ticket_runner_lib.epic_review.attempt', return_value=merge_base) as attempt:
            return review.review_resumable(rv, 'personhead'), attempt

    def test_a_blocked_review_resumes_into_the_repair_review_for_a_descendant_head(self):
        for phase in ('blocked', 'repair'):
            with self.subTest(phase=phase):
                resumable, attempt = self.resumable(phase, ancestor=True)
                self.assertTrue(resumable)
                attempt.assert_called_once_with('/repo', 'merge-base', '--is-ancestor', 'fixhead', 'personhead')

    def test_a_head_that_dropped_the_fix_tickets_gets_a_fresh_review(self):
        self.assertFalse(self.resumable('blocked', ancestor=False)[0])
        self.assertFalse(self.resumable('blocked', ancestor=True, fixHead=None)[0])

    def test_changed_requirements_or_reviewers_still_get_a_fresh_review(self):
        self.assertFalse(self.resumable('blocked', ancestor=True, fingerprint='changed')[0])
        self.assertFalse(self.resumable('blocked', ancestor=True, models=['sol'])[0])

    def test_a_mid_follow_up_review_still_needs_its_own_head(self):
        self.assertFalse(self.resumable('follow-check', ancestor=True)[0])
        self.assertTrue(self.resumable('follow-check', ancestor=True, fixHead='personhead')[0])

    def test_only_findings_no_review_has_seen_fixed_stay_open(self):
        review = EpicReview()
        confirmed = {'class': 'BLOCKING', 'outcome': 'confirmed'}
        rv = {'findings': {
            'G1': {**confirmed, 'round': 'review', 'fixes': {'sol': ['FIXED', 'ok']}},
            'G2': {**confirmed, 'round': 'review', 'fixes': {'sol': ['NOT FIXED', 'no']}},
            'G3': {**confirmed, 'round': 'follow'},
            'G4': {**confirmed, 'round': 'follow', 'repairs': [['h1', 'NOT FIXED', 'no'], ['h2', 'FIXED', 'ok']]},
            'G5': {**confirmed, 'round': 'review', 'fixes': {'sol': ['NOT FIXED', 'no']},
                   'repairs': [['h1', 'FIXED', 'ok']]},
            'G6': {'class': 'DEFERRED', 'outcome': 'confirmed', 'round': 'repair'},
            'G7': {**confirmed, 'round': 'repair'},
        }}
        self.assertEqual(review.unresolved(rv), ['G2', 'G3', 'G7'])
