"""Tests for the Devin CLI harness adapter. A fake runner and a fake detached
spawner stand in for `devin`, so no real session is ever started; the session
store is a real SQLite file under a temp directory. The fixtures mirror the
output shapes observed from devin 3000.11.92, with generic ids and paths."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import (
    HarnessError,
    InvalidRequest,
    StillListed,
    WorkspaceNotTrusted,
)
from jam.harness.claude import RunResult
from jam.harness.devin import DevinHarness
from jam.registry import Registry
from jam.service import Jam
from jam.session import SpawnOptions

from test_claude_harness import FakeRunner, ok

ROWS_V1 = [
    {"id": "alpha-fjord", "short_id": "alpha-fjord", "working_directory": "/work/repo-a", "last_activity_at": 1700000001, "title": "first"},
]
NEW_ROW = {"id": "bravo-mesa", "short_id": "bravo-mesa", "working_directory": "/work/repo-a", "last_activity_at": 1700000002, "title": "second"}
ROWS_V2 = ROWS_V1 + [NEW_ROW]

DB_SCHEMA = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, working_directory TEXT NOT NULL,"
    " backend_type TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', agent_mode TEXT NOT NULL DEFAULT '',"
    " created_at INTEGER NOT NULL, last_activity_at INTEGER NOT NULL, title TEXT,"
    " main_chain_id INTEGER, shell_last_seen_index INTEGER DEFAULT 0,"
    " cogs_json TEXT, workspace_dirs TEXT, metadata TEXT, hidden INTEGER DEFAULT 0)"
)
DB_ROWS = [
    ("alpha-fjord", "/work/repo-a", "local", 1700000001, 1700000010, "first", 0),
    ("bravo-mesa", "/work/repo-b", "local", 1700000002, 1700000020, "second", 0),
    ("charlie-hidden", "/work/repo-c", "local", 1700000003, 1700000030, "third", 1),
]


class FakeProc:
    """A detached process: `poll()` answers from a script, else reports alive."""

    def __init__(self, pid=4321, exits=None):
        self.pid = pid
        self._exits = list(exits or [])

    def poll(self):
        return self._exits.pop(0) if self._exits else None


class FakeSpawner:
    """Records the launch and returns a scripted process with a real log file."""

    def __init__(self, proc, log=""):
        self.proc = proc
        self.log = log
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w") as sink:
            sink.write(self.log)
        return self.proc, path


def make_db(path, rows=DB_ROWS):
    conn = sqlite3.connect(path)
    try:
        conn.execute(DB_SCHEMA)
        conn.executemany(
            "INSERT INTO sessions (id, working_directory, backend_type, created_at, last_activity_at, title, hidden)"
            " VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.db = self.dir / "sessions.db"

    def harness(self, *results, proc=None, log="", spawn_visible=20.0):
        spawner = FakeSpawner(proc or FakeProc(), log)
        harness = DevinHarness(
            runner=FakeRunner(*results),
            spawner=spawner,
            db_path=str(self.db),
            spawn_visible=spawn_visible,
            sleeper=lambda _: None,
        )
        return harness, spawner


class SpawnTest(Case):
    def test_spawn_runs_devin_p_in_the_directory_and_finds_the_new_id(self):
        harness, spawner = self.harness(ok(json.dumps(ROWS_V1)), ok(json.dumps(ROWS_V2)), ok(json.dumps(ROWS_V2)))
        spawned = harness.spawn(self.dir, "do the work")
        self.assertEqual(spawned.id, "bravo-mesa")
        self.assertIsNone(spawned.name)
        args, cwd = spawner.calls[0]
        self.assertEqual(args, ["devin", "-p", "do the work"])
        self.assertEqual(cwd, str(self.dir))

    def test_a_pidfile_is_recorded_for_stop(self):
        harness, _ = self.harness(ok(json.dumps(ROWS_V1)), ok(json.dumps(ROWS_V2)), ok(json.dumps(ROWS_V2)), proc=FakeProc(pid=7777))
        harness.spawn(self.dir, "do the work")
        self.assertEqual((self.dir / "jam-pids" / "bravo-mesa").read_text(), "7777")

    def test_permission_mode_is_passed_through(self):
        harness, spawner = self.harness(ok(json.dumps(ROWS_V1)), ok(json.dumps(ROWS_V2)), ok(json.dumps(ROWS_V2)))
        harness.spawn(self.dir, "do the work", permission_mode="auto")
        self.assertEqual(spawner.calls[0][0], ["devin", "--permission-mode", "auto", "-p", "do the work"])

    def test_a_name_is_refused_because_devin_picks_the_id(self):
        harness, _ = self.harness()
        with self.assertRaises(InvalidRequest):
            harness.spawn(self.dir, "do the work", name="worker-a")

    def test_it_does_not_name_sessions(self):
        self.assertIs(DevinHarness.names_sessions, False)

    def test_a_chosen_model_is_passed_as_devin_model(self):
        harness, spawner = self.harness(ok(json.dumps(ROWS_V1)), ok(json.dumps(ROWS_V2)), ok(json.dumps(ROWS_V2)))
        harness.spawn(self.dir, "do the work", options=SpawnOptions(model="claude-opus-4.6"))
        self.assertEqual(spawner.calls[0][0], ["devin", "--model", "claude-opus-4.6", "-p", "do the work"])

    def test_effort_and_a_subagent_model_are_refused_because_devin_has_neither(self):
        for options, word in ((SpawnOptions(effort="low"), "effort"), (SpawnOptions(subagent_model="haiku"), "subagent model")):
            with self.subTest(options=options):
                harness, spawner = self.harness()
                with self.assertRaises(InvalidRequest) as caught:
                    harness.spawn(self.dir, "do the work", options=options)
                self.assertIn(word, str(caught.exception))
                self.assertEqual(spawner.calls, [])

    def test_a_model_that_could_read_as_a_flag_is_refused(self):
        for model in ("-x", "--print", "has space", "", 5):
            with self.subTest(model=model):
                harness, spawner = self.harness()
                with self.assertRaises(InvalidRequest):
                    harness.spawn(self.dir, "do the work", options=SpawnOptions(model=model))
                self.assertEqual(spawner.calls, [])

    def test_spawn_validates_its_arguments(self):
        harness, _ = self.harness()
        for call in (
            lambda: harness.spawn("/no/such/dir", "do the work"),
            lambda: harness.spawn(self.dir, "   "),
            lambda: harness.spawn(self.dir, "has a \x00 byte"),
            lambda: harness.spawn(self.dir, "do the work", permission_mode="bad mode!"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(InvalidRequest):
                    call()

    def test_an_early_exit_with_an_untrusted_workspace_is_mapped(self):
        harness, _ = self.harness(
            ok(json.dumps(ROWS_V1)),
            ok(json.dumps(ROWS_V1)),
            proc=FakeProc(exits=[1]),
            log="Refusing to run in an untrusted workspace: /some/dir",
        )
        with self.assertRaises(WorkspaceNotTrusted):
            harness.spawn(self.dir, "do the work")

    def test_an_early_exit_for_another_reason_is_a_harness_error(self):
        harness, _ = self.harness(ok(json.dumps(ROWS_V1)), ok(json.dumps(ROWS_V1)), proc=FakeProc(exits=[2]), log="some failure")
        with self.assertRaisesRegex(HarnessError, "some failure"):
            harness.spawn(self.dir, "do the work")

    def test_a_session_that_never_appears_times_out(self):
        class StaticRunner:
            def __call__(self, args, cwd):
                return ok(json.dumps(ROWS_V1))

        harness = DevinHarness(
            runner=StaticRunner(),
            spawner=FakeSpawner(FakeProc()),
            db_path=str(self.db),
            spawn_visible=0.01,
            sleeper=lambda _: None,
        )
        with self.assertRaisesRegex(HarnessError, "devin list"):
            harness.spawn(self.dir, "do the work")


class ListTest(Case):
    def test_a_cwd_filter_runs_devin_list_there(self):
        runner = FakeRunner(ok(json.dumps(ROWS_V2)))
        harness = DevinHarness(runner=runner, db_path=str(self.db))
        sessions = harness.list(cwd="/work/repo-a")
        self.assertEqual(runner.calls[0], (["devin", "list", "--format", "json"], "/work/repo-a"))
        self.assertEqual([s.id for s in sessions], ["alpha-fjord", "bravo-mesa"])
        self.assertEqual(sessions[0].session_id, "alpha-fjord")
        self.assertEqual(sessions[1].name, "second")
        self.assertEqual(sessions[1].state, "resumable")

    def test_cwd_listing_skips_rows_without_an_id(self):
        harness = DevinHarness(
            runner=FakeRunner(ok(json.dumps([{"id": "alpha-fjord"}, {"title": "no id"}, "junk"]))),
            db_path=str(self.db),
        )
        self.assertEqual([s.id for s in harness.list(cwd="/work/repo-a")], ["alpha-fjord"])

    def test_without_a_cwd_the_store_is_read_and_hidden_rows_stay_hidden(self):
        make_db(self.db)
        harness = DevinHarness(db_path=str(self.db))
        sessions = harness.list()
        self.assertEqual([s.id for s in sessions], ["bravo-mesa", "alpha-fjord"])
        self.assertEqual(sessions[0].kind, "local")
        self.assertEqual(sessions[0].started_at, 1700000002)

    def test_include_exited_shows_hidden_rows(self):
        make_db(self.db)
        harness = DevinHarness(db_path=str(self.db))
        self.assertEqual([s.id for s in harness.list(include_exited=True)], ["charlie-hidden", "bravo-mesa", "alpha-fjord"])

    def test_a_missing_store_is_a_harness_error(self):
        harness = DevinHarness(db_path=str(self.dir / "no.db"))
        with self.assertRaises(HarnessError):
            harness.list()

    def test_an_invalid_cwd_filter_is_refused(self):
        harness = DevinHarness(db_path=str(self.db))
        for cwd in (5, "/a\x00b"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(InvalidRequest):
                    harness.list(cwd=cwd)


class StopTest(Case):
    def test_stop_removes_the_record_and_the_pidfile_and_reports_nothing(self):
        make_db(self.db, [])  # the record is already gone, as after a real `devin rm`
        pids = self.dir / "jam-pids"
        pids.mkdir()
        (pids / "alpha-fjord").write_text("99999999")  # a dead pid: liveness check skips it
        runner = FakeRunner(ok())
        harness = DevinHarness(runner=runner, db_path=str(self.db))
        self.assertIsNone(harness.stop("alpha-fjord"))
        self.assertEqual(runner.calls[0][0], ["devin", "rm", "alpha-fjord"])
        self.assertFalse((pids / "alpha-fjord").exists())

    def test_stop_reports_the_row_when_the_record_survives(self):
        make_db(self.db, [DB_ROWS[0]])
        harness = DevinHarness(runner=FakeRunner(ok()), db_path=str(self.db))
        self.assertEqual(harness.stop("alpha-fjord").id, "alpha-fjord")

    def test_a_failed_rm_is_a_harness_error(self):
        make_db(self.db, [DB_ROWS[0]])
        harness = DevinHarness(runner=FakeRunner(RunResult(1, "", "no such session")), db_path=str(self.db))
        with self.assertRaises(HarnessError):
            harness.stop("alpha-fjord")

    def test_ids_are_validated(self):
        harness = DevinHarness(db_path=str(self.db))
        for session_id in ("", "bad id", "/etc/passwd"):
            with self.subTest(session_id=session_id):
                with self.assertRaises(InvalidRequest):
                    harness.stop(session_id)


class CloseTest(Case):
    """`close` through the real adapter: stopping already deletes a Devin record, so there is nothing left to remove."""

    def jam(self, runner):
        return Jam(DevinHarness(runner=runner, db_path=str(self.db)), Registry(self.dir / "data"))

    def test_close_is_a_single_rm_when_stopping_leaves_no_record(self):
        make_db(self.db, [])
        runner = FakeRunner(ok())
        self.jam(runner).close("alpha-fjord")
        self.assertEqual([call[0] for call in runner.calls], [["devin", "rm", "alpha-fjord"]])

    def test_a_record_that_survives_the_stop_is_removed_and_a_second_survival_is_reported(self):
        make_db(self.db, [DB_ROWS[0]])
        runner = FakeRunner(ok(), ok())
        with self.assertRaisesRegex(HarnessError, "^the session was stopped, but removing it failed: session alpha-fjord is still listed after removal$"):
            self.jam(runner).close("alpha-fjord")
        self.assertEqual([call[0] for call in runner.calls], [["devin", "rm", "alpha-fjord"]] * 2)


class RemoveTest(Case):
    def test_remove_calls_devin_rm_and_checks_the_store(self):
        make_db(self.db, [])
        runner = FakeRunner(ok())
        harness = DevinHarness(runner=runner, db_path=str(self.db))
        harness.remove("bravo-mesa")
        self.assertEqual(runner.calls[0][0], ["devin", "rm", "bravo-mesa"])

    def test_a_record_that_survives_removal_is_still_listed(self):
        make_db(self.db, [DB_ROWS[0]])
        harness = DevinHarness(runner=FakeRunner(ok()), db_path=str(self.db))
        with self.assertRaises(StillListed):
            harness.remove("alpha-fjord")


if __name__ == "__main__":
    unittest.main()
