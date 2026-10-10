"""Tool lines keep their own colours, apart from every ticket's and stage's."""

import importlib
import os
import unittest
from unittest.mock import patch

from ticket_runner_lib import display, settings


class ColorTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(settings)
        importlib.reload(display)

    def test_tool_colors_never_match_a_ticket_or_the_agent_text(self):
        for env in ({"TERM": "xterm-256color", "COLORTERM": ""}, {"TERM": "xterm", "COLORTERM": ""}):
            with self.subTest(term=env["TERM"]), patch.dict(os.environ, env):
                importlib.reload(settings)
                tools = set(settings.TOOL_COLORS.values())
                self.assertEqual(len(tools), 4)
                self.assertFalse(tools & set(settings.TICKET_COLORS))
                self.assertNotIn(settings.AGENT_TEXT, tools | set(settings.TICKET_COLORS))
                self.assertFalse({settings.RED, settings.YELLOW} & (tools | set(settings.TICKET_COLORS)))
                self.assertEqual(len(set(settings.TICKET_COLORS)), len(settings.TICKET_COLORS))
        with patch.dict(os.environ, {"TERM": "xterm-256color"}):
            importlib.reload(settings)
            self.assertEqual(len(settings.TICKET_COLORS), 12)

    def test_each_kind_of_tool_has_one_color(self):
        importlib.reload(display)
        colors = display.TOOL_COLORS
        self.assertEqual(display.tool_color("read_file"), colors["read"])
        self.assertEqual(display.tool_color("grep"), colors["read"])
        self.assertEqual(display.tool_color("edit_file"), colors["write"])
        self.assertEqual(display.tool_color("write_file"), colors["write"])
        self.assertEqual(display.tool_color("shell_command"), colors["shell"])
        self.assertEqual(display.tool_color("mcp__chrome-devtools__click"), colors["mcp"])
