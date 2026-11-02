"""Tests for the operations table that both the CLI and the MCP server run.

Also home to the helpers the other entry-point tests share: `lazy` (import a
module on first use, so a missing module fails one test instead of a whole
file) and `runtime_for` (a Runtime over the existing fakes).
"""

from __future__ import annotations

import importlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import DiscardRequired, HarnessError, InvalidRequest, RegistryError, UnparseableOutput, WorkspaceNotTrusted
from jam.harness.claude import ClaudeHarness
from jam.registry import Registry, spawned_marked
from jam.service import Jam
from jam.session import SpawnOptions

from test_claude_harness import DISCARD, FakeRunner
from test_service import FakeHarness, StubRegistry, session

SESSION_ID = "aaaa1111-0000-4000-8000-000000000001"
HINT = "a session may have started"


class _Lazy:
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(importlib.import_module(self._name), attr)


def lazy(name):
    return _Lazy(name)


operations = lazy("jam.operations")


def identity(session_id=SESSION_ID, short="aaaa1111"):
    return lazy("jam.session").Identity(session_id, short)


def elsewhere():
    """A caller other than the session these tests close, which `close` would refuse as the caller's own."""
    return identity("cccc3333-0000-4000-8000-000000000003", "cccc3333")


def runtime_for(harness=None, registry=None, whoami=None):
    harness = FakeHarness() if harness is None else harness
    registry = StubRegistry() if registry is None else registry
    return operations.Runtime(lambda: Jam(harness, registry), identity if whoami is None else whoami)


def run_operation(name, arguments, **fakes):
    return operations.call(runtime_for(**fakes), operations.lookup(name), arguments)


def payload(outcome):
    return json.loads(outcome.text)


def raising(error):
    def fail():
        raise error

    return fail


class FailingStopHarness(FakeHarness):
    def stop(self, session_id):
        raise HarnessError("stop failed")


class VanishingHarness(FakeHarness):
    def stop(self, session_id):
        self.stop_calls.append(session_id)
        return None


# (operation, arguments, a fragment the rejection must name)
REJECTED = [
    ("spawn", {"cwd": "/work/x", "prompt": "p", "permission_mode": "default"}, "permission_mode"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "bogus": 1}, "bogus"),
    ("spawn", {"cwd": "/work/x"}, "prompt"),
    ("spawn", {"prompt": "p"}, "cwd"),
    ("spawn", {"cwd": "/work/x", "prompt": None}, "prompt"),
    ("spawn", {"cwd": 5, "prompt": "p"}, "cwd"),
    ("spawn", {"cwd": "/work/x", "prompt": ["p"]}, "prompt"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "name": 7}, "name"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "model": 7}, "model"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "effort": True}, "effort"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "subagent_model": ["haiku"]}, "subagent_model"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "model": "\ud800"}, "model"),
    ("spawn", {"cwd": "relative/dir", "prompt": "p"}, "cwd"),
    ("spawn", {"cwd": "", "prompt": "p"}, "cwd"),
    ("list", {"include_exited": "false"}, "include_exited"),
    ("list", {"include_exited": 1}, "include_exited"),
    ("list", {"cwd": "relative"}, "cwd"),
    ("list", {"cwd": 5}, "cwd"),
    ("stop", {}, "id"),
    ("stop", {"id": 5}, "id"),
    ("close", {}, "id"),
    ("close", {"id": 5}, "id"),
    ("close", {"id": "aaaa1111", "discard_unpushed": True}, "discard_unpushed"),
    ("close", {"id": "aaaa1111", "force_remove_worktree": "x"}, "force_remove_worktree"),
    ("whoami", {"anything": 1}, "anything"),
    # An unknown key is unknown whatever it holds: a null must not slip past the closed schema.
    ("spawn", {"cwd": "/work/x", "prompt": "p", "bogus": None}, "bogus"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "permission_mode": None}, "permission_mode"),
    ("list", {"bogus": None}, "bogus"),
    ("stop", {"id": "aaaa1111", "bogus": None}, "bogus"),
    ("close", {"id": "aaaa1111", "bogus": None}, "bogus"),
    ("close", {"id": "aaaa1111", "force": True}, "force"),
    ("whoami", {"anything": None}, "anything"),
    # A null stands in only for an optional parameter, never for a required one.
    ("stop", {"id": None}, "id"),
    ("close", {"id": None}, "id"),
    ("spawn", {"cwd": None, "prompt": "p"}, "cwd"),
    # An unpaired surrogate (a JSON "\ud800" escape) cannot be passed to a subprocess.
    ("spawn", {"cwd": "/work/x", "prompt": "hi \ud800"}, "prompt"),
    ("spawn", {"cwd": "/work/\ud800", "prompt": "p"}, "cwd"),
    ("spawn", {"cwd": "/work/x", "prompt": "p", "name": "\ud800"}, "name"),
    ("list", {"cwd": "/work/\ud800"}, "cwd"),
    ("stop", {"id": "\ud800"}, "id"),
    ("close", {"id": "\ud800"}, "id"),
]


class RegistryPinTest(unittest.TestCase):
    def test_the_table_names_exactly_the_verbs(self):
        self.assertEqual([op.name for op in operations.OPERATIONS], ["spawn", "list", "stop", "close", "whoami"])

    def test_lookup_returns_the_table_entry(self):
        for op in operations.OPERATIONS:
            with self.subTest(op=op.name):
                self.assertIs(operations.lookup(op.name), op)

    def test_every_schema_is_closed_and_names_the_right_keys(self):
        expected = {
            "spawn": ({"cwd", "prompt", "name", "model", "effort", "subagent_model"}, {"cwd", "prompt"}),
            "list": ({"include_exited", "cwd"}, set()),
            "stop": ({"id"}, {"id"}),
            "close": ({"id", "discard_unpushed"}, {"id"}),
            "whoami": (set(), set()),
        }
        for op in operations.OPERATIONS:
            with self.subTest(op=op.name):
                schema = operations.input_schema(op)
                properties, required = expected[op.name]
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(set(schema["properties"]), properties)
                self.assertEqual(set(schema.get("required", [])), required)
                json.dumps(schema)

    def test_property_types(self):
        spawn = operations.input_schema(operations.lookup("spawn"))["properties"]
        listing = operations.input_schema(operations.lookup("list"))["properties"]
        self.assertEqual([spawn[key]["type"] for key in ("cwd", "prompt", "name", "model", "effort", "subagent_model")], ["string"] * 6)
        self.assertEqual(listing["include_exited"]["type"], "boolean")
        self.assertEqual(listing["cwd"]["type"], "string")

    def test_permission_mode_is_not_offered(self):
        for op in operations.OPERATIONS:
            self.assertNotIn("permission_mode", operations.input_schema(op)["properties"])


class ValidatedTest(unittest.TestCase):
    def validated(self, name, arguments):
        return operations.validated(operations.lookup(name), arguments)

    def test_valid_arguments_come_back_as_given(self):
        full = {"cwd": "/work/x", "prompt": "p", "name": "n"}
        self.assertEqual(dict(self.validated("spawn", full)), full)
        self.assertEqual(dict(self.validated("list", {"include_exited": True, "cwd": "/work/x"})), {"include_exited": True, "cwd": "/work/x"})
        self.assertEqual(dict(self.validated("list", {"include_exited": False})), {"include_exited": False})
        self.assertEqual(dict(self.validated("stop", {"id": "aaaa1111"})), {"id": "aaaa1111"})
        self.assertEqual(dict(self.validated("close", {"id": "aaaa1111"})), {"id": "aaaa1111"})
        self.assertEqual(dict(self.validated("whoami", {})), {})

    def test_null_optionals_are_dropped(self):
        self.assertEqual(dict(self.validated("spawn", {"cwd": "/w", "prompt": "p", "name": None})), {"cwd": "/w", "prompt": "p"})
        self.assertEqual(dict(self.validated("list", {"include_exited": None, "cwd": None})), {})

    def test_nulls_for_required_parameters_are_not_dropped(self):
        for name, arguments, fragment in (("stop", {"id": None}, "id"), ("spawn", {"cwd": "/w", "prompt": None}, "prompt")):
            with self.subTest(name=name):
                with self.assertRaises(InvalidRequest) as caught:
                    self.validated(name, arguments)
                self.assertIn(f"{fragment} must be a string", str(caught.exception))

    def test_a_null_does_not_hide_an_unknown_key_beside_valid_ones(self):
        with self.assertRaises(InvalidRequest) as caught:
            self.validated("list", {"include_exited": None, "cwd": None, "bogus": None})
        self.assertIn("unknown argument: bogus", str(caught.exception))

    def test_rejected_arguments_raise_invalid_request_naming_the_problem(self):
        for name, arguments, fragment in REJECTED:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(InvalidRequest) as caught:
                    self.validated(name, arguments)
                self.assertIn(fragment, str(caught.exception))

    def test_the_rejection_of_an_unpaired_surrogate_does_not_echo_the_text(self):
        with self.assertRaises(InvalidRequest) as caught:
            self.validated("spawn", {"cwd": "/work/x", "prompt": "secret \ud800 text"})
        self.assertNotIn("secret", str(caught.exception))

    def test_a_raw_byte_from_the_command_line_is_not_newly_rejected(self):
        # argv bytes that are not UTF-8 arrive as U+DC80..U+DCFF and go back out as the same bytes.
        arguments = {"cwd": "/work/caf\udce9", "prompt": "raw byte \udc80"}
        self.assertEqual(dict(self.validated("spawn", arguments)), arguments)

    def test_unknown_keys_are_rejected_with_the_accepted_names(self):
        with self.assertRaises(InvalidRequest) as caught:
            self.validated("spawn", {"cwd": "/w", "prompt": "p", "permission_mode": "default"})
        text = str(caught.exception)
        self.assertIn("unknown argument", text.lower())
        for accepted in ("cwd", "prompt", "name"):
            self.assertIn(accepted, text)

    def test_non_object_arguments_are_rejected(self):
        for op in operations.OPERATIONS:
            for arguments in ([], ["cwd"], "x", 3):
                with self.subTest(op=op.name, arguments=arguments):
                    with self.assertRaises(InvalidRequest):
                        operations.validated(op, arguments)


class SpawnOperationTest(unittest.TestCase):
    def test_arguments_reach_the_harness_and_the_id_comes_back(self):
        harness, stub = FakeHarness(), StubRegistry()
        outcome = run_operation("spawn", {"cwd": "/work/x", "prompt": "do it", "name": "worker-c"}, harness=harness, registry=stub)
        self.assertIsInstance(outcome, operations.Outcome)
        self.assertTrue(outcome.ok)
        self.assertEqual(payload(outcome), {"id": "cccc3333", "name": "worker-c", "problem": None})
        self.assertEqual(list(payload(outcome)), ["id", "name", "problem"])
        self.assertEqual(harness.spawn_calls, [("/work/x", "do it", "worker-c", None)])
        self.assertEqual(stub.updates, ["cccc3333"])

    def test_the_model_effort_and_subagent_model_reach_the_harness_as_options(self):
        harness = FakeHarness()
        arguments = {"cwd": "/work/x", "prompt": "p", "model": "sonnet", "effort": "low", "subagent_model": "haiku"}
        self.assertTrue(run_operation("spawn", arguments, harness=harness).ok)
        self.assertEqual(harness.spawn_options, [SpawnOptions(model="sonnet", effort="low", subagent_model="haiku")])

    def test_omitted_or_null_options_reach_the_harness_as_none_chosen(self):
        for arguments in ({"cwd": "/work/x", "prompt": "p"}, {"cwd": "/work/x", "prompt": "p", "model": None, "effort": None, "subagent_model": None}):
            harness = FakeHarness()
            self.assertTrue(run_operation("spawn", arguments, harness=harness).ok)
            self.assertEqual(harness.spawn_options, [SpawnOptions()])

    def test_an_omitted_or_null_name_reaches_the_harness_as_none(self):
        for arguments in ({"cwd": "/work/x", "prompt": "p"}, {"cwd": "/work/x", "prompt": "p", "name": None}):
            harness = FakeHarness()
            self.assertTrue(run_operation("spawn", arguments, harness=harness).ok)
            self.assertEqual(harness.spawn_calls, [("/work/x", "p", None, None)])

    def test_a_registry_failure_after_the_spawn_keeps_the_id_and_reports_a_problem(self):
        stub = StubRegistry(update_error=RegistryError("session registry is busy"))
        outcome = run_operation("spawn", {"cwd": "/work/x", "prompt": "p"}, registry=stub)
        self.assertTrue(outcome.ok)
        self.assertEqual(payload(outcome)["id"], "cccc3333")
        self.assertIn("busy", payload(outcome)["problem"])

    def test_a_harness_failure_is_not_ok_and_records_nothing(self):
        errors = [
            (HarnessError("boom"), "boom"),
            (WorkspaceNotTrusted("/work/x"), "workspace not trusted"),
            (UnparseableOutput("no id", "raw output"), "raw output"),
            (InvalidRequest("bad"), "bad"),
            (RuntimeError("/private/path"), "RuntimeError"),
        ]
        for error, fragment in errors:
            with self.subTest(error=type(error).__name__):
                stub = StubRegistry()
                outcome = run_operation("spawn", {"cwd": "/work/x", "prompt": "p"}, harness=FakeHarness(spawn_error=error), registry=stub)
                self.assertFalse(outcome.ok)
                self.assertIn(fragment, outcome.text)
                self.assertNotIn("/private/path", outcome.text)
                self.assertEqual(stub.updates, [])

    def test_base_exceptions_are_not_caught(self):
        with self.assertRaises(KeyboardInterrupt):
            run_operation("spawn", {"cwd": "/work/x", "prompt": "p"}, harness=FakeHarness(spawn_error=KeyboardInterrupt()))

    def test_the_real_harness_rejects_a_missing_directory_without_the_hint(self):
        runner = FakeRunner()
        runtime = operations.Runtime(lambda: Jam(ClaudeHarness(runner=runner), StubRegistry()), identity)
        outcome = operations.call(runtime, operations.lookup("spawn"), {"cwd": "/work/does-not-exist", "prompt": "p"})
        self.assertFalse(outcome.ok)
        self.assertIn("not a directory", outcome.text)
        self.assertNotIn(HINT, outcome.text)
        self.assertEqual(runner.calls, [])


class RejectedArgumentsTest(unittest.TestCase):
    def test_nothing_reaches_the_harness_or_the_registry(self):
        for name, arguments, fragment in REJECTED + [("spawn", [], ""), ("list", "x", ""), ("stop", 3, ""), ("close", 3, "")]:
            with self.subTest(name=name, arguments=arguments):
                harness, stub = FakeHarness(), StubRegistry()
                outcome = run_operation(name, arguments, harness=harness, registry=stub)
                self.assertFalse(outcome.ok)
                self.assertIn(fragment, outcome.text)
                self.assertNotIn(HINT, outcome.text)
                self.assertEqual((harness.spawn_calls, harness.list_calls, harness.stop_calls, harness.remove_calls), ([], [], [], []))
                self.assertEqual((stub.updates, stub.records), ([], []))


class ListOperationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_spawned_is_true_false_or_unknown_and_harness_order_is_kept(self):
        registry = Registry(self.root / "data")
        registry.update("bbbb2222", spawned_marked)
        registry.update("cccc3333", lambda raw: {**raw, "other": 1})
        harness = FakeHarness([session("cccc3333"), session("aaaa1111"), session("bbbb2222")])
        outcome = run_operation("list", {}, harness=harness, registry=registry)
        rows = payload(outcome)["sessions"]
        self.assertEqual([row["id"] for row in rows], ["cccc3333", "aaaa1111", "bbbb2222"])
        self.assertEqual([row["spawned"] for row in rows], [False, None, True])
        self.assertIsNone(payload(outcome)["problem"])
        self.assertEqual(list(payload(outcome)), ["sessions", "problem"])

    def test_rows_carry_the_documented_fields_only(self):
        busy = session("aaaa1111", status="busy", pid=4242, started_at=1700000000000)
        outcome = run_operation("list", {}, harness=FakeHarness([busy, session("bbbb2222")]))
        first, second = payload(outcome)["sessions"]
        self.assertEqual(list(first), ["id", "session_id", "name", "cwd", "kind", "state", "status", "spawned"])
        self.assertEqual((first["status"], second["status"]), ("busy", None))
        self.assertEqual((first["session_id"], first["cwd"], first["kind"], first["state"]), ("aaaa1111-full", "/work/x", "background", "done"))

    def test_filters_reach_the_harness(self):
        harness = FakeHarness()
        run_operation("list", {"include_exited": True, "cwd": "/work/x"}, harness=harness)
        run_operation("list", {}, harness=harness)
        self.assertEqual(harness.list_calls, [(True, "/work/x"), (False, None)])

    def test_a_registry_problem_comes_back_with_the_rows_intact(self):
        stub = StubRegistry(record_error=RegistryError("cannot read the session registry: Input/output error"))
        outcome = run_operation("list", {}, harness=FakeHarness([session("aaaa1111"), session("bbbb2222")]), registry=stub)
        self.assertTrue(outcome.ok)
        self.assertEqual([row["id"] for row in payload(outcome)["sessions"]], ["aaaa1111", "bbbb2222"])
        self.assertEqual([row["spawned"] for row in payload(outcome)["sessions"]], [None, None])
        self.assertIn("cannot read", payload(outcome)["problem"])

    def test_a_harness_failure_is_not_ok(self):
        outcome = run_operation("list", {}, harness=FakeHarness(list_error=HarnessError("down")))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.text, "down")


class CloseOperationTest(unittest.TestCase):
    def test_close_reports_the_id_and_never_touches_the_registry(self):
        harness, stub = FakeHarness([session("aaaa1111")]), StubRegistry()
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, registry=stub, whoami=elsewhere)
        self.assertTrue(outcome.ok)
        self.assertEqual(payload(outcome), {"id": "aaaa1111", "closed": True})
        self.assertEqual((harness.stop_calls, harness.remove_calls), (["aaaa1111"], ["aaaa1111"]))
        self.assertEqual((stub.updates, stub.records, stub.removes), ([], [], []))

    def test_a_session_that_stopping_already_deleted_is_closed_without_a_removal(self):
        harness = VanishingHarness()
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=elsewhere)
        self.assertEqual(payload(outcome), {"id": "aaaa1111", "closed": True})
        self.assertEqual((harness.stop_calls, harness.remove_calls), (["aaaa1111"], []))

    def test_a_failed_stop_is_a_failure_and_nothing_is_removed(self):
        harness = FailingStopHarness()
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=elsewhere)
        self.assertEqual((outcome.ok, outcome.text), (False, "stop failed"))
        self.assertEqual(harness.remove_calls, [])

    def test_a_refusal_is_a_failure_that_keeps_the_harness_text_and_says_what_was_done(self):
        harness = FakeHarness([session("aaaa1111")], remove_error=HarnessError("removing aaaa1111 failed (exit 1): unpushed commits"))
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=elsewhere)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.text, "the session was stopped, but removing it failed: removing aaaa1111 failed (exit 1): unpushed commits")

    def test_a_very_long_refusal_is_clipped_after_the_note_that_the_session_was_stopped(self):
        harness = FakeHarness([session("aaaa1111")], remove_error=HarnessError("x" * 5000))
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=elsewhere)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.text.startswith("the session was stopped, but removing it failed: x"))
        self.assertLessEqual(len(outcome.text), 2000)

    def test_the_callers_own_session_is_refused_and_nothing_runs(self):
        harness = FakeHarness([session("aaaa1111")])
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness)
        self.assertFalse(outcome.ok)
        self.assertIn("own session", outcome.text)
        self.assertEqual((harness.stop_calls, harness.remove_calls), ([], []))

    def test_a_caller_the_harness_cannot_name_is_no_session_so_the_close_goes_ahead(self):
        # whoami cannot answer on the Devin CLI or from a plain shell; then no listed session is the caller's own.
        harness = FakeHarness([session("aaaa1111")])
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=raising(HarnessError("no session id")))
        self.assertTrue(outcome.ok, outcome.text)
        self.assertEqual(harness.remove_calls, ["aaaa1111"])

    def test_a_discard_value_reaches_the_harness(self):
        harness = FakeHarness([session("aaaa1111")])
        outcome = run_operation("close", {"id": "aaaa1111", "discard_unpushed": DISCARD}, harness=harness, whoami=elsewhere)
        self.assertTrue(outcome.ok, outcome.text)
        self.assertEqual(harness.discards, [DISCARD])

    def test_a_refusal_that_offers_a_discard_value_ends_with_it_and_when_to_pass_it_even_when_clipped(self):
        harness = FakeHarness([session("aaaa1111")], remove_error=DiscardRequired("x" * 5000, DISCARD))
        outcome = run_operation("close", {"id": "aaaa1111"}, harness=harness, whoami=elsewhere)
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.text.startswith("the session was stopped, but removing it failed: x"))
        self.assertLessEqual(len(outcome.text), 2000)
        tail = outcome.text[outcome.text.index(" | "):]
        for phrase in (f"discard_unpushed {DISCARD}", "--discard-unpushed", "merged", "the owner"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, tail)


class StopAndWhoamiTest(unittest.TestCase):
    def test_stop_returns_the_row_and_never_touches_the_registry(self):
        harness, stub = FakeHarness(), StubRegistry()
        outcome = run_operation("stop", {"id": "aaaa1111"}, harness=harness, registry=stub)
        self.assertTrue(outcome.ok)
        self.assertEqual(list(payload(outcome)), ["id", "session"])
        self.assertEqual(payload(outcome)["id"], "aaaa1111")
        self.assertEqual((payload(outcome)["session"]["id"], payload(outcome)["session"]["state"]), ("aaaa1111", "done"))
        self.assertEqual(harness.stop_calls, ["aaaa1111"])
        self.assertEqual((stub.updates, stub.records, stub.removes), ([], [], []))

    def test_stop_reports_null_when_the_harness_lists_no_row(self):
        outcome = run_operation("stop", {"id": "aaaa1111"}, harness=VanishingHarness())
        self.assertEqual(payload(outcome), {"id": "aaaa1111", "session": None})

    def test_whoami_returns_the_identity(self):
        outcome = run_operation("whoami", {})
        self.assertEqual(payload(outcome), {"session_id": SESSION_ID, "id": "aaaa1111"})
        self.assertEqual(list(payload(outcome)), ["session_id", "id"])
        outcome = run_operation("whoami", {}, whoami=lambda: identity(short=None))
        self.assertEqual(payload(outcome), {"session_id": SESSION_ID, "id": None})

    def test_whoami_is_a_failure_when_the_identity_source_raises(self):
        outcome = run_operation("whoami", {}, whoami=raising(HarnessError("CLAUDE_CODE_SESSION_ID is not set")))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.text, "CLAUDE_CODE_SESSION_ID is not set")

    def test_whoami_needs_no_jam_and_the_others_survive_a_broken_factory(self):
        runtime = operations.Runtime(raising(RuntimeError("/private/path")), identity)
        self.assertTrue(operations.call(runtime, operations.lookup("whoami"), {}).ok)
        for name, arguments in (("spawn", {"cwd": "/work/x", "prompt": "p"}), ("list", {}), ("stop", {"id": "aaaa1111"}), ("close", {"id": "aaaa1111"})):
            with self.subTest(name=name):
                outcome = operations.call(runtime, operations.lookup(name), arguments)
                self.assertFalse(outcome.ok)
                self.assertIn("internal error: RuntimeError", outcome.text)
                self.assertNotIn("/private/path", outcome.text)


class HintScopeTest(unittest.TestCase):
    def test_the_run_list_hint_belongs_to_spawn_failures_only(self):
        spawn = {"cwd": "/work/x", "prompt": "p"}
        timeout = HarnessError("'claude' timed out after 60s")
        self.assertIn(HINT, run_operation("spawn", spawn, harness=FakeHarness(spawn_error=timeout)).text)
        self.assertIn(HINT, run_operation("spawn", spawn, harness=FakeHarness(spawn_error=RuntimeError("x"))).text)
        for error in (InvalidRequest("bad"), WorkspaceNotTrusted("/work/x")):
            self.assertNotIn(HINT, run_operation("spawn", spawn, harness=FakeHarness(spawn_error=error)).text)
        failures = [
            run_operation("list", {}, harness=FakeHarness(list_error=timeout)),
            run_operation("stop", {"id": "aaaa1111"}, harness=FailingStopHarness()),
            run_operation("close", {"id": "aaaa1111"}, harness=FakeHarness(remove_error=HarnessError("refused"))),
            run_operation("close", {"id": "aaaa1111"}, harness=FailingStopHarness()),
            run_operation("whoami", {}, whoami=raising(HarnessError("not set"))),
        ]
        for outcome in failures:
            self.assertFalse(outcome.ok)
            self.assertNotIn(HINT, outcome.text)


if __name__ == "__main__":
    unittest.main()
