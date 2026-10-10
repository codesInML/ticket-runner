"""Focused checks for report parsing and review input boundaries."""
from types import SimpleNamespace
import unittest

from ticket_runner_lib.epic_review import EpicReview
from ticket_runner_lib.text import review_rows


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

    def test_repair_verdict_cannot_hide_blocking_findings(self):
        _, problem = EpicReview().parse_findings(
            '## FINDINGS\n- R1 BLOCKING fixed.py:1 — wrong — code evidence\nVERDICT=PASS',
            {'letter': 'R'}, r'VERDICT=(PASS|FAIL)')
        self.assertIn('contradictory', problem)
