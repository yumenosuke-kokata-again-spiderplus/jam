"""Checks that the documents say what the model choice on spawn (D18) and the project prefix on names (D19) really do.

Same style as test_spawn_docs.py: flattened text, one section at a time.
"""

from __future__ import annotations

import unittest

from test_skill import parts
from test_spawn_docs import DECISIONS, README, flat, section


class DecisionsTest(unittest.TestCase):
    def test_d18_says_why_the_model_may_choose_what_it_may_not_for_a_permission_mode(self):
        d18 = flat(section(DECISIONS, "D18."))
        self.assertIn("change what a session costs and how well it reasons, never what it may do", d18)
        self.assertIn("`--settings` with a JSON string gives that one process the variable and writes no file", d18)
        self.assertIn("Devin has no per-session effort and no subagent model, so either is refused before anything runs", d18)
        self.assertIn("On Claude Code an unknown name is not refused", d18)

    def test_d12_points_from_the_hidden_permission_mode_to_the_choices_that_are_offered(self):
        d12 = flat(section(DECISIONS, "D12."))
        self.assertIn("The model, effort and subagent model are offered, because they do not change what a session may do (D18).", d12)

    def test_d19_names_the_main_checkout_and_leaves_an_unnamed_session_unnamed(self):
        d19 = flat(section(DECISIONS, "D19."))
        self.assertIn("A linked worktree therefore names its main checkout", d19)
        self.assertIn("When the caller gives no name, jam passes none.", d19)
        self.assertIn("The caller's part is never cut", d19)


class ReadmeAndSkillTest(unittest.TestCase):
    def test_the_readme_says_spawn_takes_the_model_and_names_the_project(self):
        self.assertIn("on the model and effort the task calls for (D18), with the project's name in front of the session's name (D19)", flat(README))

    def test_the_skill_gives_the_generic_rule_for_choosing_and_asks_for_a_name(self):
        text = flat(parts()[1])
        for phrase in (
            "Give a short `name` for the task (jam puts the project in front)",
            "a stronger model for design and hard debugging, a cheaper one for docs and mechanical edits",
            "a small `subagent_model` for a session that will fan work out to many subagents",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
