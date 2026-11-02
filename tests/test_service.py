"""Tests for the service that composes a harness with the registry."""

import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import HarnessError, InvalidRequest, RegistryError, StillListed, UnparseableOutput, WorkspaceNotTrusted
from jam.harness.claude import ClaudeHarness
from jam.registry import Record, Registry, spawned_marked
from jam.service import Jam, Listed, Listing, SpawnResult, failure_reason, joined
from jam.session import NO_OPTIONS, Session, SpawnOptions, Spawned

from test_claude_harness import LIVE, STOPPED, FakeRunner, agents, ok
from jam.harness.claude import RunResult

SPAWNED = Spawned(id="cccc3333", name="worker-c")


def session(session_id, **fields):
    return Session(id=session_id, session_id=session_id + "-full", name="n", cwd="/work/x", kind="background", state="done", **fields)


class FakeHarness:
    def __init__(self, sessions=(), spawn_error=None, list_error=None, on_spawn=None, remove_error=None, names_sessions=False):
        self.sessions = list(sessions)
        self.spawn_error, self.list_error, self.on_spawn, self.remove_error = spawn_error, list_error, on_spawn, remove_error
        self.names_sessions = names_sessions
        self.spawn_calls, self.list_calls, self.stop_calls, self.remove_calls = [], [], [], []
        self.spawn_options = []

    def spawn(self, cwd, prompt, name=None, permission_mode=None, options=NO_OPTIONS):
        self.spawn_calls.append((cwd, prompt, name, permission_mode))
        self.spawn_options.append(options)
        if self.on_spawn:
            self.on_spawn()
        if self.spawn_error:
            raise self.spawn_error
        return SPAWNED

    def list(self, include_exited=False, cwd=None):
        self.list_calls.append((include_exited, cwd))
        if self.list_error:
            raise self.list_error
        return list(self.sessions)

    def stop(self, session_id):
        self.stop_calls.append(session_id)
        return session(session_id)

    def remove(self, session_id):
        self.remove_calls.append(session_id)
        if self.remove_error:
            raise self.remove_error


class StubRegistry:
    """Counts calls and optionally raises, standing in for Registry."""

    def __init__(self, update_error=None, record_error=None):
        self.update_error, self.record_error = update_error, record_error
        self.updates, self.records, self.removes = [], [], []

    def update(self, key, change):
        self.updates.append(key)
        if self.update_error:
            raise self.update_error

    def record(self, key):
        self.records.append(key)
        if self.record_error:
            raise self.record_error
        return None

    def remove(self, key):
        self.removes.append(key)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = self.root / "data"


class SpawnTest(ServiceTestCase):
    def test_spawn_records_spawned_and_passes_arguments_through(self):
        harness, registry = FakeHarness(), Registry(self.data)
        result = Jam(harness, registry).spawn("/work/x", "do it", name="worker-c", permission_mode="default")
        self.assertEqual(result, SpawnResult(SPAWNED, None))
        self.assertEqual(harness.spawn_calls, [("/work/x", "do it", "worker-c", "default")])
        self.assertEqual(registry.record("cccc3333"), Record(spawned=True))

    def test_the_id_survives_a_registry_error(self):
        harness, registry = FakeHarness(), StubRegistry(update_error=RegistryError("session registry is busy"))
        result = Jam(harness, registry).spawn("/work/x", "do it")
        self.assertEqual(result.spawned.id, "cccc3333")
        self.assertIn("cccc3333", result.problem)
        self.assertIn("busy", result.problem)
        self.assertEqual(len(harness.spawn_calls), 1)

    def test_the_id_survives_a_bug_without_leaking_its_message(self):
        registry = StubRegistry(update_error=RuntimeError("/private/path"))
        result = Jam(FakeHarness(), registry).spawn("/work/x", "do it")
        self.assertEqual(result.spawned.id, "cccc3333")
        self.assertIn("RuntimeError", result.problem)
        self.assertNotIn("/private/path", result.problem)

    def test_keyboard_interrupt_is_not_swallowed(self):
        registry = StubRegistry(update_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            Jam(FakeHarness(), registry).spawn("/work/x", "do it")

    def test_a_failed_spawn_propagates_and_leaves_the_registry_untouched(self):
        errors = [
            WorkspaceNotTrusted("/work/x"),
            UnparseableOutput("no id", "raw output"),
            HarnessError("boom"),
            InvalidRequest("bad"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                stub = StubRegistry()
                with self.assertRaises(type(error)) as caught:
                    Jam(FakeHarness(spawn_error=error), stub).spawn("/work/x", "do it")
                if isinstance(error, UnparseableOutput):
                    self.assertEqual(caught.exception.raw, "raw output")
                self.assertEqual(stub.updates, [])
                self.assertFalse(self.data.exists())

    def test_the_options_reach_the_harness_as_given(self):
        harness, options = FakeHarness(), SpawnOptions(model="sonnet", effort="low", subagent_model="haiku")
        Jam(harness, StubRegistry()).spawn("/work/x", "do it", options=options)
        self.assertEqual(harness.spawn_options, [options])

    def test_without_options_the_harness_gets_none_chosen(self):
        harness = FakeHarness()
        Jam(harness, StubRegistry()).spawn("/work/x", "do it")
        self.assertEqual(harness.spawn_options, [NO_OPTIONS])

    def test_a_harness_that_names_sessions_gets_the_name_with_the_project_in_front(self):
        project = self.root / "shop"
        (project / "src").mkdir(parents=True)
        (project / ".git").mkdir()
        harness = FakeHarness(names_sessions=True)
        Jam(harness, StubRegistry()).spawn(str(project / "src"), "do it", name="fix-tests")
        self.assertEqual(harness.spawn_calls, [(str(project / "src"), "do it", "shop-fix-tests", None)])

    def test_the_claude_adapter_gets_the_prefixed_name_and_the_chosen_options(self):
        project = self.root / "shop"
        (project / ".git").mkdir(parents=True)
        runner = FakeRunner(ok("backgrounded · cccc3333 · shop-fix\n"))
        result = Jam(ClaudeHarness(claude_bin="claude", runner=runner), StubRegistry()).spawn(
            str(project), "do it", name="fix", options=SpawnOptions(model="haiku", effort="low")
        )
        self.assertEqual(runner.calls[0][0], ["claude", "--bg", "-n", "shop-fix", "--model", "haiku", "--effort", "low", "--", "do it"])
        self.assertEqual(result.spawned.name, "shop-fix")

    def test_a_harness_that_names_sessions_gets_no_name_when_none_was_given(self):
        harness = FakeHarness(names_sessions=True)
        Jam(harness, StubRegistry()).spawn(str(self.root), "do it")
        self.assertEqual(harness.spawn_calls, [(str(self.root), "do it", None, None)])

    def test_a_harness_that_does_not_name_sessions_gets_the_name_as_given(self):
        harness = FakeHarness(names_sessions=False)
        Jam(harness, StubRegistry()).spawn(str(self.root), "do it", name="fix-tests")
        self.assertEqual(harness.spawn_calls, [(str(self.root), "do it", "fix-tests", None)])

    def test_the_registry_is_usable_while_the_harness_spawns(self):
        outcome = []

        def other_writer():
            Registry(self.data, lock_timeout=0.1).update("zzzz9999", spawned_marked)
            outcome.append("ok")

        Jam(FakeHarness(on_spawn=other_writer), Registry(self.data)).spawn("/work/x", "do it")
        self.assertEqual(outcome, ["ok"])


class ListTest(ServiceTestCase):
    def test_list_joins_registry_notes_onto_harness_rows_in_harness_order(self):
        registry = Registry(self.data)
        registry.update("bbbb2222", spawned_marked)
        registry.update("orphan99", spawned_marked)
        harness = FakeHarness([session("aaaa1111"), session("bbbb2222")])
        listing = Jam(harness, registry).list(include_exited=True, cwd="/work/x")
        self.assertEqual([row.session.id for row in listing.sessions], ["aaaa1111", "bbbb2222"])
        self.assertEqual([row.record for row in listing.sessions], [None, Record(spawned=True)])
        self.assertIsNone(listing.problem)
        self.assertEqual(harness.list_calls, [(True, "/work/x")])

    def test_list_degrades_visibly_when_the_registry_fails(self):
        stub = StubRegistry(record_error=RegistryError("cannot read the session registry: Input/output error"))
        listing = Jam(FakeHarness([session("aaaa1111")]), stub).list()
        self.assertEqual([row.record for row in listing.sessions], [None])
        self.assertIn("cannot read", listing.problem)

    def test_list_lets_a_registry_bug_through_instead_of_degrading_on_it(self):
        # Only RegistryError is an expected, message-safe failure; anything else is a bug to surface.
        stub = StubRegistry(record_error=RuntimeError("/private/path"))
        with self.assertRaises(RuntimeError):
            Jam(FakeHarness([session("aaaa1111")]), stub).list()

    def test_list_survives_an_odd_harness_id(self):
        listing = Jam(FakeHarness([session("../x")]), Registry(self.data)).list()
        self.assertEqual(listing.sessions[0].record, None)
        self.assertIsNone(listing.problem)

    def test_a_harness_failure_propagates_and_the_registry_is_not_consulted(self):
        stub = StubRegistry()
        with self.assertRaises(HarnessError):
            Jam(FakeHarness(list_error=HarnessError("down")), stub).list()
        self.assertEqual(stub.records, [])

    def test_joined_is_a_pure_left_join(self):
        rows = [session("aaaa1111"), session("bbbb2222")]
        self.assertEqual(
            joined(rows, {"bbbb2222": Record(spawned=True), "orphan99": Record(spawned=True)}),
            (Listed(rows[0], None), Listed(rows[1], Record(spawned=True))),
        )

    def test_spawn_then_list_with_a_real_registry(self):
        registry = Registry(self.data)
        harness = FakeHarness([session("cccc3333")])
        jam = Jam(harness, registry)
        jam.spawn("/work/x", "do it")
        (row,) = jam.list().sessions
        self.assertTrue(row.record.spawned)

    def test_list_works_with_the_real_claude_harness(self):
        harness = ClaudeHarness(claude_bin="claude", runner=FakeRunner(agents(LIVE)))
        listing = Jam(harness, Registry(self.data)).list()
        self.assertEqual([row.session.id for row in listing.sessions], ["aaaa1111"])
        self.assertIsInstance(listing, Listing)


class StopTest(ServiceTestCase):
    def test_stop_delegates_and_never_touches_the_registry(self):
        harness, stub = FakeHarness(), StubRegistry()
        stopped = Jam(harness, stub).stop("aaaa1111")
        self.assertEqual(stopped.id, "aaaa1111")
        self.assertEqual(harness.stop_calls, ["aaaa1111"])
        self.assertEqual((stub.updates, stub.records, stub.removes), ([], [], []))


class GoneAfterStopHarness(FakeHarness):
    """Like the Devin adapter: stopping a session deletes it, so no row is left to report."""

    def stop(self, session_id):
        self.stop_calls.append(session_id)
        return None


class StoppingFailsHarness(FakeHarness):
    def stop(self, session_id):
        self.stop_calls.append(session_id)
        raise HarnessError("stopping aaaa1111 failed (exit 1): no such session")


ALL = ["claude", "agents", "--json", "--all"]
LIVE_STOPPED = {key: value for key, value in LIVE.items() if key not in ("pid", "status")}
STOPPED_A = {**LIVE_STOPPED, "state": "stopped"}


class CloseTest(ServiceTestCase):
    def test_a_session_is_stopped_and_then_removed_without_asking_for_a_listing(self):
        harness, stub = FakeHarness([session("aaaa1111")]), StubRegistry()
        self.assertIsNone(Jam(harness, stub).close("aaaa1111"))
        self.assertEqual((harness.stop_calls, harness.remove_calls, harness.list_calls), (["aaaa1111"], ["aaaa1111"], []))
        self.assertEqual((stub.updates, stub.records, stub.removes), ([], [], []))

    def test_nothing_is_removed_when_stopping_leaves_no_row(self):
        harness = GoneAfterStopHarness()
        Jam(harness, StubRegistry()).close("aaaa1111")
        self.assertEqual((harness.stop_calls, harness.remove_calls), (["aaaa1111"], []))

    def test_a_failed_stop_is_reported_as_it_is_and_nothing_is_removed(self):
        harness = StoppingFailsHarness()
        with self.assertRaisesRegex(HarnessError, "no such session") as caught:
            Jam(harness, StubRegistry()).close("aaaa1111")
        self.assertNotIn("was stopped", str(caught.exception))
        self.assertEqual(harness.remove_calls, [])

    def test_a_refused_removal_says_the_session_was_stopped(self):
        refusal = HarnessError("removing aaaa1111 failed (exit 1): the worktree has unpushed commits")
        harness = FakeHarness([session("aaaa1111")], remove_error=refusal)
        with self.assertRaises(HarnessError) as caught:
            Jam(harness, StubRegistry()).close("aaaa1111")
        self.assertEqual(
            str(caught.exception),
            "the session was stopped, but removing it failed: removing aaaa1111 failed (exit 1): the worktree has unpushed commits",
        )
        self.assertIs(caught.exception.__cause__, refusal)

    def test_a_session_that_stays_listed_after_removal_is_reported_after_the_stop(self):
        harness = FakeHarness([session("aaaa1111")], remove_error=StillListed("aaaa1111"))
        with self.assertRaisesRegex(HarnessError, r"^the session was stopped, but removing it failed: session aaaa1111 is still listed after removal$"):
            Jam(harness, StubRegistry()).close("aaaa1111")


class CloseOnClaudeTest(ServiceTestCase):
    """The commands a close runs on the real adapter, in order, against a scripted `claude`."""

    def close(self, *results):
        runner = FakeRunner(*results)
        jam = Jam(ClaudeHarness(claude_bin="claude", runner=runner), Registry(self.data))
        return jam, runner

    def sequence(self, runner):
        return [call[0] for call in runner.calls]

    def test_a_running_session_is_stopped_then_removed_and_the_removal_is_checked(self):
        jam, runner = self.close(ok("stopped aaaa1111"), agents(STOPPED_A), ok("removed aaaa1111"), agents())
        jam.close("aaaa1111")
        self.assertEqual(self.sequence(runner), [["claude", "stop", "aaaa1111"], ALL, ["claude", "rm", "aaaa1111"], ALL])

    def test_a_session_that_is_already_stopped_goes_through_the_same_steps(self):
        # `claude stop` on a stopped row that is still listed exits 0 (observed on 2.1.309, D17), so there is no branch for it.
        jam, runner = self.close(ok("stopped bbbb2222"), agents(STOPPED), ok("removed bbbb2222"), agents())
        jam.close("bbbb2222")
        self.assertEqual(self.sequence(runner), [["claude", "stop", "bbbb2222"], ALL, ["claude", "rm", "bbbb2222"], ALL])

    def test_a_session_gone_once_stopped_is_not_removed(self):
        jam, runner = self.close(ok("stopped aaaa1111"), agents())
        jam.close("aaaa1111")
        self.assertEqual(self.sequence(runner), [["claude", "stop", "aaaa1111"], ALL])

    def test_a_refusal_comes_back_with_the_harness_text_and_no_force_is_tried(self):
        refusal = RunResult(1, "", "refusing to remove: the worktree has 2 unpushed commits")
        jam, runner = self.close(ok("stopped aaaa1111"), agents(STOPPED_A), refusal)
        with self.assertRaises(HarnessError) as caught:
            jam.close("aaaa1111")
        self.assertIn("refusing to remove: the worktree has 2 unpushed commits", str(caught.exception))
        self.assertTrue(str(caught.exception).startswith("the session was stopped, but removing it failed: "))
        self.assertEqual(self.sequence(runner), [["claude", "stop", "aaaa1111"], ALL, ["claude", "rm", "aaaa1111"]])

    def test_an_unknown_id_is_the_harness_failure_and_nothing_else_runs(self):
        jam, runner = self.close(RunResult(1, "", "No job matching 'aaaa1111'"))
        with self.assertRaisesRegex(HarnessError, "No job matching 'aaaa1111'") as caught:
            jam.close("aaaa1111")
        self.assertNotIn("was stopped", str(caught.exception))
        self.assertEqual(self.sequence(runner), [["claude", "stop", "aaaa1111"]])

    def test_a_prefix_of_an_id_is_rejected_because_claude_would_resolve_it(self):
        jam, runner = self.close()
        with self.assertRaises(InvalidRequest):
            jam.close("aaaa")
        self.assertEqual(runner.calls, [])

    def test_an_invalid_id_is_rejected_before_anything_runs(self):
        for bad in ("", "-x", "a b", "a;b", "x" * 65):
            with self.subTest(bad=bad):
                jam, runner = self.close()
                with self.assertRaises(InvalidRequest):
                    jam.close(bad)
                self.assertEqual(runner.calls, [])


class FailureReasonTest(unittest.TestCase):
    def test_only_registry_errors_may_carry_their_message(self):
        self.assertEqual(failure_reason(RegistryError("busy")), "busy")
        self.assertEqual(failure_reason(RuntimeError("/private/path")), "RuntimeError")


class NothingCreatedTest(ServiceTestCase):
    def test_listing_never_creates_the_data_dir(self):
        Jam(FakeHarness([session("aaaa1111")]), Registry(self.data)).list()
        self.assertFalse(self.data.exists())


if __name__ == "__main__":
    unittest.main()
