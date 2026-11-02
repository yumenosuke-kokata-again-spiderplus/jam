"""Checks that the documents describe the `close` verb as it is, and that the sentences it replaced are gone.

Same style as test_spawn_docs.py: flattened text, one section at a time, and
both the new wording and the removed wording pinned.
"""

from __future__ import annotations

import unittest

from test_spawn_docs import DECISIONS, README, flat, section


class ReadmeTest(unittest.TestCase):
    def test_the_verb_is_in_the_design_the_status_and_the_feature_list(self):
        design = flat(section(README, "Design"))
        self.assertIn("verbs: spawn / list / stop / close / whoami", design)
        self.assertIn("`spawn`, `list`, `stop`, `close`, and `whoami` as tools", design)
        self.assertIn("with `spawn`, `list`, `stop`, `close`, and `whoami`", flat(section(README, "Status")))
        self.assertIn("**close** it, which also deletes it, when the owner asks for that or has said to (D17)", flat(section(README, "What it does")))
        self.assertIn("A session cannot close itself, and work that exists nowhere else is deleted only with the value its refusal named.", flat(README))
        for removed in ("verbs: spawn / list / stop / whoami", "`stop`, and `whoami` as tools", "`stop`, and `whoami`, plus three"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, flat(README))


class DecisionsTest(unittest.TestCase):
    def test_every_statement_of_the_verb_set_names_close(self):
        text = flat(DECISIONS)
        for phrase in (
            "the verbs `spawn`, `list`, `stop`, `close`, and `whoami` (D12 shows how the entry points expose them; `close` is D17)",
            "`spawn`, `list`, `stop`, `close`, and `whoami` are rows of one operations table",
            "and `stop`, `close` and `whoami` never touch the registry",
            "`mcp__plugin_jam_jam__spawn`, `__list`, `__stop`, `__close`, and `__whoami`",
            "a `prune` verb (a `remove` one was added as `close`, D17)",
            "closing one only when the owner asks (D17)",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        for removed in (
            "the verbs `spawn`, `list`, `stop`, and `whoami`",
            "`spawn`, `list`, `stop`, and `whoami` are rows",
            "a `remove` or `prune` verb",
            "`stop` and `whoami` never touch the registry",
            "the four commands, the MCP tool names",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, text)

    def test_d17_records_what_close_does_and_who_may_use_it(self):
        d17 = flat(section(DECISIONS, "D17."))
        for phrase in (
            "Decided on 2026-10-29, from issue #32 and the owner's request.",
            "`close` takes one short `id`",
            "It stops the session, then deletes it through the adapter's `remove`, which re-checks the full listing.",
            'The result is `{"id", "closed": true}`, and anything else is a failure.',
            "Stopping comes first for every session, with no check of whether it is running",
            "a Devin row never carries a `status`",
            "A session that is no longer listed once it is stopped is not deleted a second time",
            "An id the harness does not know fails at the stop, in the harness's words.",
            "Only when the owner asked to close or remove the session,",
            "not on its own initiative",
            "Nothing in code can tell who is asking (sessions are peers, D10)",
            "or told the model to close the sessions it starts once their work is done (added on 2026-11-02, issue #37)",
            "jam never passes `--force-remove-worktree`, and passes `--discard-unpushed` only with a value the caller gives as `discard_unpushed`",
            "The value named after `--discard-unpushed` comes back apart from the reason, at the end of the failure where clipping cannot cut it",
            'behind "the session was stopped, but removing it failed: "',
            "The note leads so that clipping a long message cannot drop it",
            "`claude rm` reports a refusal on stdout, not stderr, together with the commands that override it",
            "drops any line that names `--discard-unpushed`, `--force-remove-worktree`, `ctrl+x` or the `claude rm` command",
            "The Claude adapter passes on only the whole short id (eight hexadecimal characters, the form of the spawn output)",
            "a prefix would stop one session, find no row with that exact id, and read as a session that stopping had deleted",
            "on Claude Code it keeps the conversation",
            "On the Devin CLI stopping already deletes the record (D16)",
            "`close` leaves the registry alone, as `stop` does",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, d17)
        for removed in ("was_running", "looks the id up in the full listing", "An id that is not listed is a failure", "comes back as it is", "stopped but not removed", "jam passes neither", "no live refusal was seen"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, d17)

    def test_d17_records_the_additions_of_issues_37_and_38(self):
        d17 = flat(section(DECISIONS, "D17."))
        for phrase in (
            "**The caller's own session is refused (2026-11-02, issue #37).**",
            "Where `whoami` cannot answer (the Devin CLI, a plain shell), no session is the caller's own and the close goes on.",
            "**Why the discard value is relayed (2026-11-02, issue #37).**",
            "the commit it names is the head of a merged pull request, or the owner said to drop the work",
            "**The lock of the stopping process (2026-11-02).**",
            "A lock still held at the bound is reported as it is",
            "**A stopped session that comes back (2026-11-02, issue #37).**",
            "and does not wake it",
            "What tells the supervisor to restore a stopped session was not found.",
            "**Verified (2026-11-02, Claude Code 2.1.295).**",
            "The wait and the single retry are covered by unit tests only.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, d17)

    def test_d17_names_what_it_rejected_and_what_it_did_not_verify(self):
        d17 = flat(section(DECISIONS, "D17."))
        for phrase in (
            "A flag on `stop` that also deletes",
            "A bulk verb that removes every stopped session",
            "Documenting the owner's own `claude rm` only",
            "Skipping the stop for a session that is not running, which the first draft did by reading a missing `status` as stopped",
            "would have lost its record and its pidfile and gone on running",
            "**Verified (2026-10-29, Claude Code 2.1.309).**",
            "A second `claude stop` of a stopped, still listed session printed `stopped <id>` and exited 0.",
            "A real refusal, over unpushed commits or uncommitted changes, was not provoked here, so the stdout form of its text is unobserved",
            "`claude rm` on a running session without a stop was not tried",
            "The Devin side is covered by unit tests only, through the real adapter over a scripted `devin` and a temporary store.",
            "Observed once and not explained",
            "so a session can vanish between the listing and the deletion",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, d17)


if __name__ == "__main__":
    unittest.main()
