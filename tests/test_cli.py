"""Tests for the argparse CLI. Every test passes its own env, streams and runtime builder."""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import HarnessError, RegistryError
from jam.registry import Record, Registry
from jam.session import Session, SpawnOptions

from test_claude_harness import DISCARD, SPAWN_STDOUT, FakeRunner, ok
from test_operations import HINT, SESSION_ID, FailingStopHarness, elsewhere, lazy, operations, runtime_for
from test_service import FakeHarness, StubRegistry, session

cli = lazy("jam.cli")
compose = lazy("jam.compose")
mcp = lazy("jam.mcp")

ROOT = Path(__file__).resolve().parent.parent
SESSION_VAR, JOB_VAR, DATA_VAR = "CLAUDE_CODE_SESSION_ID", "CLAUDE_JOB_DIR", "CLAUDE_PLUGIN_DATA"
TOKEN_VAR, TOKEN = "CLAUDE_CODE_SESSION_ACCESS_TOKEN", "planted-token-value"
SPAWN = ["spawn", "--cwd", "/work/x", "--"]


def unused_build(*args, **kwargs):
    raise AssertionError("the runtime must not be built here")


def text_stream(data=b""):
    """A text stream over bytes, so it works whether the CLI reads `.buffer` or iterates text."""
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", newline="")


def run_cli(argv, runtime=None, env=None, stdin=b"", build=None):
    out, err = io.StringIO(), io.StringIO()
    if build is None:
        build = unused_build if runtime is None else (lambda *args, **kwargs: runtime)
    code = cli.main(argv, env={} if env is None else env, stdin=text_stream(stdin), stdout=out, stderr=err, build=build)
    return code, out.getvalue(), err.getvalue()


def mcp_text(name, arguments, runtime):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    reply = json.loads(mcp.handle_line(runtime, json.dumps(message).encode("utf-8"), "1.0.0"))
    (content,) = reply["result"]["content"]
    return content["text"]


class SubcommandTest(unittest.TestCase):
    def subparsers(self):
        parser = cli.build_parser()
        (action,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        return action.choices

    def test_the_commands_are_the_operations_plus_mcp(self):
        self.assertEqual(set(self.subparsers()), {op.name for op in operations.OPERATIONS} | {"mcp"})

    def test_each_schema_property_is_a_dest_of_its_subparser(self):
        for op in operations.OPERATIONS:
            with self.subTest(op=op.name):
                dests = {a.dest for a in self.subparsers()[op.name]._actions if not isinstance(a, argparse._HelpAction)}
                self.assertEqual(dests, set(operations.input_schema(op)["properties"]))


class SpawnCommandTest(unittest.TestCase):
    def test_arguments_reach_the_core_and_one_json_line_comes_out(self):
        harness, stub = FakeHarness(), StubRegistry()
        code, out, err = run_cli(["spawn", "--cwd", "/work/x", "--name", "worker-c", "--", "do it"], runtime_for(harness, stub))
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out), {"id": "cccc3333", "name": "worker-c", "problem": None})
        self.assertEqual(harness.spawn_calls, [("/work/x", "do it", "worker-c", None)])
        self.assertEqual(stub.updates, ["cccc3333"])

    def test_the_model_effort_and_subagent_model_flags_reach_the_core(self):
        harness = FakeHarness()
        argv = ["spawn", "--cwd", "/work/x", "--model", "sonnet", "--effort", "low", "--subagent-model", "haiku", "--", "do it"]
        code, out, err = run_cli(argv, runtime_for(harness))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(harness.spawn_options, [SpawnOptions(model="sonnet", effort="low", subagent_model="haiku")])

    def test_dash_led_and_multi_line_prompts_arrive_unchanged(self):
        for prompt in ("-fix it", "-h", "--name", "line one\nline two\n", "日本語の指示\n二行目"):
            with self.subTest(prompt=prompt):
                harness = FakeHarness()
                code, out, err = run_cli([*SPAWN, prompt], runtime_for(harness))
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(harness.spawn_calls, [("/work/x", prompt, None, None)])

    def test_a_registry_problem_still_exits_zero_with_the_id(self):
        stub = StubRegistry(update_error=RegistryError("session registry is busy"))
        code, out, err = run_cli([*SPAWN, "do it"], runtime_for(registry=stub))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], "cccc3333")
        self.assertIn("busy", json.loads(out)["problem"])

    def test_a_failure_writes_one_jam_line_to_stderr_and_nothing_to_stdout(self):
        harness = FakeHarness(spawn_error=HarnessError("boom"))
        code, out, err = run_cli([*SPAWN, "do it"], runtime_for(harness))
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("jam: boom"))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn(HINT, err)

    def test_a_relative_cwd_is_an_operation_failure_that_never_reaches_the_harness(self):
        harness = FakeHarness()
        code, out, err = run_cli(["spawn", "--cwd", "relative", "--", "do it"], runtime_for(harness))
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("jam: "))
        self.assertEqual(harness.spawn_calls, [])


class OtherCommandsTest(unittest.TestCase):
    def test_list_passes_its_filters(self):
        harness = FakeHarness([session("aaaa1111")])
        code, out, err = run_cli(["list", "--include-exited", "--cwd", "/work/x"], runtime_for(harness))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([row["id"] for row in json.loads(out)["sessions"]], ["aaaa1111"])
        run_cli(["list"], runtime_for(harness))
        self.assertEqual(harness.list_calls, [(True, "/work/x"), (False, None)])

    def test_list_failures_have_no_hint(self):
        code, out, err = run_cli(["list"], runtime_for(FakeHarness(list_error=HarnessError("down"))))
        self.assertEqual((code, out, err), (1, "", "jam: down\n"))

    def test_stop_prints_the_row(self):
        harness = FakeHarness()
        code, out, err = run_cli(["stop", "aaaa1111"], runtime_for(harness))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["id"], "aaaa1111")
        self.assertEqual(json.loads(out)["session"]["id"], "aaaa1111")
        self.assertEqual(harness.stop_calls, ["aaaa1111"])
        code, out, err = run_cli(["stop", "aaaa1111"], runtime_for(FailingStopHarness()))
        self.assertEqual((code, out, err), (1, "", "jam: stop failed\n"))

    def test_close_prints_the_result_and_a_refusal_is_a_failure(self):
        harness = FakeHarness([session("aaaa1111")])
        code, out, err = run_cli(["close", "aaaa1111"], runtime_for(harness, whoami=elsewhere))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"id": "aaaa1111", "closed": True})
        self.assertEqual((harness.stop_calls, harness.remove_calls), (["aaaa1111"], ["aaaa1111"]))
        refused = FakeHarness([session("aaaa1111")], remove_error=HarnessError("worktree has unpushed commits"))
        self.assertEqual(
            run_cli(["close", "aaaa1111"], runtime_for(refused, whoami=elsewhere)),
            (1, "", "jam: the session was stopped, but removing it failed: worktree has unpushed commits\n"),
        )

    def test_close_takes_a_discard_value_as_a_flag_and_refuses_the_callers_own_session(self):
        harness = FakeHarness([session("aaaa1111")])
        code, _, err = run_cli(["close", "aaaa1111", "--discard-unpushed", DISCARD], runtime_for(harness, whoami=elsewhere))
        self.assertEqual((code, err, harness.discards), (0, "", [DISCARD]))
        own = FakeHarness([session("aaaa1111")])
        code, out, err = run_cli(["close", "aaaa1111"], runtime_for(own))
        self.assertEqual((code, out, own.stop_calls), (1, "", []))
        self.assertIn("own session", err)

    def test_whoami_prints_the_identity_from_the_injected_environment(self):
        env = {SESSION_VAR: SESSION_ID, JOB_VAR: "/jobs/aaaa1111", TOKEN_VAR: TOKEN}
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["whoami"], env=env, stdin=text_stream(), stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"session_id": SESSION_ID, "id": "aaaa1111"})
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_whoami_prints_a_null_id_for_a_job_directory_inherited_from_another_session(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["whoami"], env={SESSION_VAR: SESSION_ID, JOB_VAR: "/jobs/bbbb2222"}, stdin=text_stream(), stdout=out, stderr=err)
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(json.loads(out.getvalue()), {"session_id": SESSION_ID, "id": None})

    def test_whoami_without_a_session_id_fails_naming_the_variable_only(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["whoami"], env={TOKEN_VAR: TOKEN}, stdin=text_stream(), stdout=out, stderr=err)
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertTrue(err.getvalue().startswith("jam: "))
        self.assertIn(SESSION_VAR, err.getvalue())
        self.assertNotIn(TOKEN, err.getvalue())


class ParityTest(unittest.TestCase):
    def test_stdout_is_byte_identical_to_the_mcp_tool_text(self):
        busy = session("aaaa1111", status="busy", pid=1)
        cases = [
            (["spawn", "--cwd", "/work/x", "--name", "worker-c", "--", "do it"], "spawn", {"cwd": "/work/x", "prompt": "do it", "name": "worker-c"}),
            (["spawn", "--cwd", "/work/x", "--model", "opus", "--effort", "max", "--subagent-model", "sonnet", "--", "p"], "spawn", {"cwd": "/work/x", "prompt": "p", "model": "opus", "effort": "max", "subagent_model": "sonnet"}),
            (["list", "--include-exited"], "list", {"include_exited": True}),
            (["list", "--cwd", "/work/x"], "list", {"cwd": "/work/x"}),
            (["stop", "aaaa1111"], "stop", {"id": "aaaa1111"}),
            # Not `aaaa1111`, the caller's own session, which close refuses.
            (["close", "bbbb2222"], "close", {"id": "bbbb2222"}),
            (["close", "bbbb2222", "--discard-unpushed", DISCARD], "close", {"id": "bbbb2222", "discard_unpushed": DISCARD}),
            (["whoami"], "whoami", {}),
        ]
        for argv, name, arguments in cases:
            with self.subTest(command=name):
                _, out, _ = run_cli(argv, runtime_for(FakeHarness([busy]), StubRegistry()))
                self.assertEqual(out, mcp_text(name, arguments, runtime_for(FakeHarness([busy]), StubRegistry())) + "\n")


class UsageTest(unittest.TestCase):
    BAD = [
        [],
        ["bogus"],
        ["spawn"],
        ["spawn", "--", "do it"],
        ["spawn", "--cwd", "/work/x"],
        ["spawn", "--cwd", "/work/x", "--permission-mode", "default", "--", "do it"],
        ["spawn", "--cwd", "/work/x", "--na", "n", "--", "do it"],
        ["list", "--include"],
        ["list", "--data-dir", "/d"],
        ["--data", "/d", "list"],
        ["stop"],
        ["close"],
        ["close", "aaaa1111", "--force"],
        ["whoami", "extra"],
        ["spawn", "--cwd", "/work/x", "--", "an", "unquoted", "brief"],
    ]
    COMMANDS = ("spawn", "list", "stop", "close", "whoami")

    def test_usage_errors_return_two_with_usage_on_stderr_and_nothing_on_stdout(self):
        for argv in self.BAD:
            with self.subTest(argv=argv):
                code, out, err = run_cli(argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("jam: usage error:", err)
                self.assertIn("usage:", err)
                self.assertNotIn("Traceback", err)

    def test_the_usage_line_is_the_one_of_the_command_that_failed(self):
        for argv in self.BAD:
            with self.subTest(argv=argv):
                command = argv[0] if argv and argv[0] in self.COMMANDS else None
                expected = "usage: jam [-h]" if command is None else f"usage: jam {command} [-h]"
                _, _, err = run_cli(argv)
                self.assertTrue(err.startswith(expected), err)

    def test_an_unquoted_brief_shows_the_spawn_usage_with_its_prompt(self):
        _, _, err = run_cli([*SPAWN, "an", "unquoted", "brief"])
        self.assertTrue(err.startswith("usage: jam spawn"), err)
        usage = err.split("jam: usage error", 1)[0]  # argparse wraps a long usage line
        self.assertIn("PROMPT", usage)
        self.assertIn("unrecognized arguments: unquoted brief", err)

    def test_permission_mode_is_named_in_the_error(self):
        code, _, err = run_cli([*SPAWN[:3], "--permission-mode", "default", "--", "p"])
        self.assertEqual(code, 2)
        self.assertIn("--permission-mode", err)

    def test_help_exits_zero_on_the_injected_stdout(self):
        code, out, err = run_cli(["--help"])
        self.assertEqual((code, err), (0, ""))
        for command in ("spawn", "list", "stop", "close", "whoami"):
            self.assertIn(command, out)

    def test_mcp_is_not_advertised_in_the_command_list(self):
        _, out, _ = run_cli(["--help"])
        self.assertIsNone(re.search(r"\bmcp\b", out), out)

    def test_the_help_spells_arguments_as_the_flags_this_parser_accepts(self):
        for command in ("list", "stop", "close"):
            with self.subTest(command=command):
                _, out, _ = run_cli([command, "--help"])
                self.assertNotIn("include_exited", out)
        self.assertIn("--include-exited", run_cli(["list", "--help"])[1])
        self.assertIn("--include-exited", run_cli(["stop", "--help"])[1])

    def test_spawn_help_shows_the_flags_and_the_brief_recipe_but_not_a_permission_mode(self):
        code, out, err = run_cli(["spawn", "--help"])
        self.assertEqual((code, err), (0, ""))
        for fragment in ("--cwd", "--name", "--model", "--effort", "--subagent-model", "BRIEF"):
            self.assertIn(fragment, out)
        self.assertNotIn("--permission-mode", out)
        self.assertNotIn("subagent_model", out)


class DataDirTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_flag_reaches_the_builder(self):
        seen = []

        def build(*args, **kwargs):
            seen.append([*args, *kwargs.values()])
            return runtime_for()

        run_cli(["--data-dir", "/some/dir", "list"], build=build)
        self.assertEqual(len(seen), 1)
        self.assertIn("/some/dir", seen[0])

    def test_the_flag_beats_the_environment_variable(self):
        flag_dir, env_dir = self.root / "flag", self.root / "env"
        runner = FakeRunner(ok(SPAWN_STDOUT))

        def build(*args, **kwargs):
            return compose.build_runtime(*args, runner=runner, **kwargs)

        code, out, err = run_cli(["--data-dir", str(flag_dir), "spawn", "--cwd", str(self.root), "--", "do it"], env={DATA_VAR: str(env_dir)}, build=build)
        self.assertEqual((code, err), (0, ""))
        self.assertIsNone(json.loads(out)["problem"])
        self.assertEqual(Registry(flag_dir).record("cccc3333"), Record(spawned=True))
        self.assertFalse(env_dir.exists())

    def test_a_missing_data_directory_degrades_to_a_problem_not_a_failure(self):
        runner = FakeRunner(ok(SPAWN_STDOUT))
        build = lambda *args, **kwargs: compose.build_runtime(*args, runner=runner, **kwargs)  # noqa: E731
        code, out, err = run_cli(["spawn", "--cwd", str(self.root), "--", "do it"], build=build)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["id"], "cccc3333")
        self.assertIn("--data-dir", json.loads(out)["problem"])


class McpCommandTest(unittest.TestCase):
    def test_the_mcp_command_serves_over_the_injected_streams(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        data = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
        code, out, err = run_cli(["mcp"], runtime_for(), stdin=data)
        self.assertEqual((code, err), (0, ""))
        first, second = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(out.count("\n"), 2)
        self.assertEqual(first["result"]["serverInfo"]["name"], "jam")
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(first["result"]["serverInfo"]["version"], manifest["version"])
        self.assertEqual([tool["name"] for tool in second["result"]["tools"]], ["spawn", "list", "stop", "close", "whoami"])

    def test_a_tool_call_over_the_mcp_command_reaches_the_runtime(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list", "arguments": {}}}
        harness = FakeHarness([session("aaaa1111")])
        code, out, err = run_cli(["mcp"], runtime_for(harness), stdin=(json.dumps(message) + "\n").encode("utf-8"))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(harness.list_calls), 1)
        self.assertFalse(json.loads(out)["result"]["isError"])


class ReconfigurableStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.reconfigured = []

    def reconfigure(self, **kwargs):
        self.reconfigured.append(kwargs)


class RealStdoutTest(unittest.TestCase):
    def test_the_real_stdout_is_switched_to_utf8_when_it_can_be(self):
        stream = ReconfigurableStream()
        with mock.patch.object(sys, "stdout", stream):
            code = cli.main(["list"], env={}, stdin=text_stream(), stderr=io.StringIO(), build=lambda *a, **k: runtime_for())
        self.assertEqual(code, 0)
        self.assertIn({"encoding": "utf-8", "errors": "replace"}, stream.reconfigured)
        self.assertEqual(json.loads(stream.getvalue())["sessions"], [])

    def test_a_non_ascii_cwd_survives_a_non_utf8_locale(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii")
        harness = FakeHarness([Session(id="aaaa1111", session_id="s", name="n", cwd="/work/日本語", kind="background", state="done")])
        with mock.patch.object(sys, "stdout", stream):
            code = cli.main(["list"], env={}, stdin=text_stream(), stderr=io.StringIO(), build=lambda *a, **k: runtime_for(harness))
        stream.flush()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw.getvalue().decode("utf-8"))["sessions"][0]["cwd"], "/work/日本語")


if __name__ == "__main__":
    unittest.main()
