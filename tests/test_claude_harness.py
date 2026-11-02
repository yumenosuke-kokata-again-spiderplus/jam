"""Tests for the Claude Code harness adapter. A fake runner stands in for the
`claude` binary, so no real session is ever started. The fixtures mirror the
output shapes observed from Claude Code 2.1.300, with generic ids and paths."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import (
    HarnessError,
    InvalidRequest,
    StillListed,
    UnparseableOutput,
    WorkspaceNotTrusted,
)
from jam.harness.claude import ClaudeHarness, RunResult, run_subprocess
from jam.session import SpawnOptions

LIVE = {
    "pid": 4242,
    "id": "aaaa1111",
    "cwd": "/work/repo-a",
    "kind": "background",
    "startedAt": 1700000000000,
    "sessionId": "aaaa1111-0000-4000-8000-000000000001",
    "name": "worker-a",
    "status": "busy",
    "state": "working",
}
# A stopped session keeps its row in `agents --json --all` but loses pid/status.
STOPPED = {
    "id": "bbbb2222",
    "cwd": "/work/repo-b",
    "kind": "background",
    "startedAt": 1700000001000,
    "sessionId": "bbbb2222-0000-4000-8000-000000000002",
    "name": "worker-b",
    "state": "stopped",
}
SPAWN_STDOUT = (
    "backgrounded · \x1b[36mcccc3333\x1b[39m · worker-c\n"
    "\x1b[2m  claude agents             list sessions\x1b[22m\n"
    "\x1b[2m  claude stop cccc3333      stop this session\x1b[22m\n"
)
UNTRUSTED = (
    "Workspace not trusted. Run `claude` in /private/tmp/untrusted-dir once and "
    "accept the trust prompt, then retry."
)


class FakeRunner:
    """Records every call and answers from a script of RunResults (in order)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        return self.results.pop(0)


def ok(stdout="", stderr=""):
    return RunResult(0, stdout, stderr)


def agents(*rows):
    return ok(json.dumps(list(rows)))


class SpawnTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()

    def harness(self, *results):
        runner = FakeRunner(*results)
        return ClaudeHarness(claude_bin="claude", runner=runner), runner

    def test_builds_argv_with_prompt_after_double_dash(self):
        harness, runner = self.harness(ok(SPAWN_STDOUT))
        harness.spawn(self.dir, "do the thing", name="worker-c", permission_mode="default")
        args, cwd = runner.calls[0]
        self.assertEqual(
            args,
            ["claude", "--bg", "-n", "worker-c", "--permission-mode", "default", "--", "do the thing"],
        )
        self.assertEqual(cwd, str(self.dir))

    def test_the_chosen_model_effort_and_subagent_model_go_before_the_prompt(self):
        harness, runner = self.harness(ok(SPAWN_STDOUT))
        harness.spawn(self.dir, "do it", name="worker-c", options=SpawnOptions(model="claude-sonnet-4.5", effort="xhigh", subagent_model="haiku"))
        args = runner.calls[0][0]
        self.assertEqual(
            args,
            [
                "claude", "--bg", "-n", "worker-c",
                "--model", "claude-sonnet-4.5", "--effort", "xhigh",
                "--settings", '{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}}',
                "--", "do it",
            ],
        )
        self.assertEqual(json.loads(args[args.index("--settings") + 1]), {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})

    def test_each_option_is_passed_on_its_own(self):
        cases = [
            (SpawnOptions(model="opus"), ["--model", "opus"]),
            (SpawnOptions(effort="max"), ["--effort", "max"]),
            (SpawnOptions(subagent_model="sonnet"), ["--settings", '{"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}}']),
        ]
        for options, flags in cases:
            with self.subTest(options=options):
                harness, runner = self.harness(ok(SPAWN_STDOUT))
                harness.spawn(self.dir, "x", options=options)
                self.assertEqual(runner.calls[0][0], ["claude", "--bg", *flags, "--", "x"])

    def test_it_names_sessions(self):
        self.assertIs(ClaudeHarness.names_sessions, True)

    def test_omits_optional_flags_and_keeps_a_leading_dash_prompt_safe(self):
        harness, runner = self.harness(ok(SPAWN_STDOUT))
        harness.spawn(self.dir, "-looks like a flag")
        self.assertEqual(runner.calls[0][0], ["claude", "--bg", "--", "-looks like a flag"])

    def test_parses_id_and_name_from_decorated_output(self):
        harness, _ = self.harness(ok(SPAWN_STDOUT))
        spawned = harness.spawn(self.dir, "x", name="worker-c")
        self.assertEqual((spawned.id, spawned.name), ("cccc3333", "worker-c"))

    def test_parses_output_without_a_name(self):
        harness, _ = self.harness(ok("backgrounded · dddd4444\n  claude agents   list sessions\n"))
        spawned = harness.spawn(self.dir, "x")
        self.assertEqual((spawned.id, spawned.name), ("dddd4444", None))

    def test_unparseable_output_with_exit_zero_carries_the_raw_text(self):
        harness, _ = self.harness(ok("something unexpected\n"))
        with self.assertRaises(UnparseableOutput) as caught:
            harness.spawn(self.dir, "x")
        self.assertIn("something unexpected", caught.exception.raw)

    def test_untrusted_workspace_reports_the_path_from_the_message(self):
        harness, _ = self.harness(RunResult(1, "", UNTRUSTED))
        with self.assertRaises(WorkspaceNotTrusted) as caught:
            harness.spawn(self.dir, "x")
        self.assertEqual(caught.exception.path, "/private/tmp/untrusted-dir")

    def test_untrusted_match_is_loose_and_falls_back_to_the_requested_dir(self):
        harness, _ = self.harness(RunResult(1, "", "ERROR: this workspace is NOT TRUSTED yet"))
        with self.assertRaises(WorkspaceNotTrusted) as caught:
            harness.spawn(self.dir, "x")
        self.assertEqual(caught.exception.path, str(self.dir))

    def test_untrusted_path_is_read_from_stdout_when_the_message_is_there(self):
        harness, _ = self.harness(RunResult(1, UNTRUSTED.replace("untrusted-dir", "from-stdout"), ""))
        with self.assertRaises(WorkspaceNotTrusted) as caught:
            harness.spawn(self.dir, "x")
        self.assertEqual(caught.exception.path, "/private/tmp/from-stdout")

    def test_stderr_wins_when_both_streams_mention_the_failure(self):
        noisy_stderr = "warning: something happened in a place once"
        harness, _ = self.harness(RunResult(1, UNTRUSTED, noisy_stderr + "\n" + "not trusted"))
        with self.assertRaises(WorkspaceNotTrusted) as caught:
            harness.spawn(self.dir, "x")
        self.assertEqual(caught.exception.path, "a place")

    def test_other_failures_surface_stderr_without_retry(self):
        harness, runner = self.harness(RunResult(2, "", "boom"))
        with self.assertRaises(HarnessError) as caught:
            harness.spawn(self.dir, "x")
        self.assertNotIsInstance(caught.exception, WorkspaceNotTrusted)
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_requests_never_reach_the_runner(self):
        harness, runner = self.harness()
        cases = [
            dict(cwd=self.dir / "missing", prompt="x"),
            dict(cwd=self.dir, prompt=""),
            dict(cwd=self.dir, prompt="   "),
            dict(cwd=self.dir, prompt="a\x00b"),
            dict(cwd=self.dir, prompt="x", name="-flag"),
            dict(cwd=self.dir, prompt="x", name="has space"),
            dict(cwd=self.dir, prompt="x", name="n" * 65),
            dict(cwd=self.dir, prompt="x", permission_mode="--bad"),
            # A model must never read as a flag, and effort is one of the levels the harness knows.
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model="-x")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model="--settings")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model="has space")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model="m" * 65)),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model="")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(effort="huge")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(effort="LOW")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(subagent_model="-x")),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(subagent_model='a"b')),
            # Wrong types must be the same error, not a TypeError from the regex or Path.
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(model=5)),
            dict(cwd=self.dir, prompt="x", options=SpawnOptions(effort=["low"])),
            dict(cwd=self.dir, prompt="x", options="sonnet"),
            dict(cwd=self.dir, prompt="x", name=123),
            dict(cwd=self.dir, prompt="x", permission_mode=123),
            dict(cwd=123, prompt="x"),
            dict(cwd=None, prompt="x"),
            dict(cwd=self.dir, prompt=None),
        ]
        for kwargs in cases:
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(InvalidRequest):
                    harness.spawn(**kwargs)
        self.assertEqual(runner.calls, [])

    def test_runner_errors_such_as_timeouts_propagate(self):
        def runner(args, cwd):
            raise HarnessError("timed out")

        with self.assertRaises(HarnessError):
            ClaudeHarness(runner=runner).spawn(self.dir, "x")


class ListTest(unittest.TestCase):
    def harness(self, *results):
        runner = FakeRunner(*results)
        return ClaudeHarness(claude_bin="claude", runner=runner), runner

    def test_lists_live_sessions(self):
        harness, runner = self.harness(agents(LIVE))
        (session,) = harness.list()
        self.assertEqual(runner.calls[0][0], ["claude", "agents", "--json"])
        self.assertEqual(
            (session.id, session.session_id, session.name, session.cwd, session.kind),
            ("aaaa1111", LIVE["sessionId"], "worker-a", "/work/repo-a", "background"),
        )
        self.assertEqual((session.status, session.state, session.pid), ("busy", "working", 4242))

    def test_stopped_sessions_lack_status_and_pid(self):
        harness, _ = self.harness(agents(STOPPED))
        (session,) = harness.list(include_exited=True)
        self.assertEqual((session.status, session.pid, session.state), (None, None, "stopped"))

    def test_a_non_string_cwd_filter_is_an_invalid_request(self):
        harness, runner = self.harness()
        for bad in (123, ["/work"], "a\x00b"):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidRequest):
                    harness.list(cwd=bad)
        self.assertEqual(runner.calls, [])

    def test_include_exited_and_cwd_become_flags(self):
        harness, runner = self.harness(agents())
        harness.list(include_exited=True, cwd="/work/repo-a")
        self.assertEqual(runner.calls[0][0], ["claude", "agents", "--json", "--all", "--cwd", "/work/repo-a"])

    def test_tolerates_drift_in_keys_and_values(self):
        drifted = dict(LIVE, id="eeee5555", kind="something-new", state="a-state-we-never-saw", extra=[1, 2])
        harness, _ = self.harness(agents(drifted, {"no": "id"}, "not-an-object"))
        sessions = harness.list()
        self.assertEqual([s.id for s in sessions], ["eeee5555"])
        self.assertEqual((sessions[0].kind, sessions[0].state), ("something-new", "a-state-we-never-saw"))

    def test_unusable_output_is_a_harness_error(self):
        for result in (ok("not json"), ok('{"a": 1}'), RunResult(1, "", "denied")):
            with self.subTest(stdout=result.stdout[:10]):
                harness, _ = self.harness(result)
                with self.assertRaises(HarnessError):
                    harness.list()


class StopAndRemoveTest(unittest.TestCase):
    def harness(self, *results):
        runner = FakeRunner(*results)
        return ClaudeHarness(claude_bin="claude", runner=runner), runner

    def test_stop_returns_the_observed_stopped_row(self):
        harness, runner = self.harness(ok("stopped bbbb2222"), agents(STOPPED))
        observed = harness.stop("bbbb2222")
        self.assertEqual(runner.calls[0][0], ["claude", "stop", "bbbb2222"])
        self.assertEqual(runner.calls[1][0], ["claude", "agents", "--json", "--all"])
        self.assertEqual((observed.id, observed.state), ("bbbb2222", "stopped"))

    def test_stop_returns_none_when_the_session_is_no_longer_listed(self):
        harness, _ = self.harness(ok("stopped bbbb2222"), agents(LIVE))
        self.assertIsNone(harness.stop("bbbb2222"))

    def test_stop_failure_is_a_harness_error(self):
        harness, _ = self.harness(RunResult(1, "", "no such session"))
        with self.assertRaises(HarnessError):
            harness.stop("bbbb2222")

    def test_remove_verifies_absence_against_the_full_listing(self):
        harness, runner = self.harness(ok("removed bbbb2222"), agents(LIVE))
        harness.remove("bbbb2222")
        self.assertEqual(runner.calls[0][0], ["claude", "rm", "bbbb2222"])
        self.assertEqual(runner.calls[1][0], ["claude", "agents", "--json", "--all"])

    def test_remove_reports_a_session_that_is_still_listed(self):
        harness, _ = self.harness(ok("removed bbbb2222"), agents(STOPPED))
        with self.assertRaises(StillListed):
            harness.remove("bbbb2222")

    def test_a_refusal_reported_on_stdout_gives_its_reason_without_the_commands_that_override_it(self):
        refusal = (
            "kept bbbb2222 \u2014 its worktree is still at /work/wt\n"
            "The worktree has 2 unpushed commits.\n"
            "claude rm bbbb2222 --discard-unpushed abc1234@def5678\n"
            "or delete the session from 'claude agents' (ctrl+x twice) to discard them\n"
            "run claude rm bbbb2222 --force-remove-worktree def5678 to delete the directory\n"
        )
        harness, _ = self.harness(RunResult(1, refusal, ""))
        with self.assertRaises(HarnessError) as caught:
            harness.remove("bbbb2222")
        text = str(caught.exception)
        self.assertTrue(text.startswith("removing bbbb2222 failed (exit 1): kept bbbb2222"), text)
        self.assertIn("The worktree has 2 unpushed commits.", text)
        for override in ("--discard-unpushed", "--force-remove-worktree", "ctrl+x", "claude rm bbbb2222"):
            with self.subTest(override=override):
                self.assertNotIn(override, text)

    def test_a_refusal_with_stderr_and_stdout_keeps_both_reasons(self):
        harness, _ = self.harness(RunResult(1, "kept bbbb2222 \u2014 its worktree is still at /work/wt\n", "couldn't remove /work/wt"))
        with self.assertRaisesRegex(HarnessError, r"couldn't remove /work/wt \| kept bbbb2222"):
            harness.remove("bbbb2222")

    def test_a_failure_whose_output_is_only_overrides_says_nothing_more_than_the_exit_status(self):
        harness, _ = self.harness(RunResult(1, "claude rm bbbb2222 --discard-unpushed abc@def\n", ""))
        with self.assertRaises(HarnessError) as caught:
            harness.remove("bbbb2222")
        self.assertEqual(str(caught.exception), "removing bbbb2222 failed (exit 1)")

    def test_session_ids_are_validated_before_anything_runs(self):
        harness, runner = self.harness()
        # `claude stop` resolves a prefix of an id, so only the short id itself may reach it (a prefix would stop one session and look gone).
        for bad in ("", "-x", "a b", "a;b", "x" * 65, "aaaa", "AAAA1111", "aaaa11112", "aaaa1111-0000-4000-8000-000000000001", "worker-a"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRequest):
                    harness.stop(bad)
                with self.assertRaises(InvalidRequest):
                    harness.remove(bad)
        self.assertEqual(runner.calls, [])


class SubprocessRunnerTest(unittest.TestCase):
    """The default runner, exercised with the Python interpreter as a stand-in
    for `claude`."""

    def test_captures_exit_code_stdout_and_stderr(self):
        code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
        result = run_subprocess([sys.executable, "-c", code], None)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr.strip()), (3, "out", "err"))

    def test_stdin_is_closed_so_a_waiting_child_cannot_hang(self):
        result = run_subprocess([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], None)
        self.assertEqual(result.stdout.strip(), "''")

    def test_timeout_becomes_a_harness_error_without_leaking_arguments(self):
        sentinel = "do-not-leak-this-prompt"
        with self.assertRaises(HarnessError) as caught:
            run_subprocess([sys.executable, "-c", "import time; time.sleep(5)", sentinel], None, timeout=0.3)
        self.assertNotIn(sentinel, str(caught.exception))

    def test_missing_binary_becomes_a_harness_error(self):
        with self.assertRaises(HarnessError):
            run_subprocess(["jam-test-no-such-binary"], None)

    def test_runs_in_the_requested_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_subprocess([sys.executable, "-c", "import os; print(os.getcwd())"], tmp)
            self.assertEqual(Path(result.stdout.strip()).resolve(), Path(tmp).resolve())


if __name__ == "__main__":
    unittest.main()
