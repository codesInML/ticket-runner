"""The extracted implementation must remain protected from agent edits."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ticket_runner_lib.runner import Runner
from ticket_runner_lib.settings import DEFAULTS, Fatal


class RunnerProtectionTests(unittest.TestCase):
    def test_agent_editing_an_extracted_module_stops_the_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runner = Runner(
                SimpleNamespace(workflow="pocock", parent=207), root, root,
                root / "state.json", {"_meta": {"branch": "test"}}, {}, [],
                root / "SKILL.md", root, DEFAULTS,
            )
            module = Path(__file__).resolve().parents[1] / "ticket_runner_lib/verification.py"
            self.assertIn(module, runner.snapshots)
            # Simulate an edit without modifying the actual runner on disk.
            runner.snapshots[module] += b"\n# agent edit\n"
            with self.assertRaisesRegex(Fatal, "Agent modified .*verification.py"):
                runner.guard()
