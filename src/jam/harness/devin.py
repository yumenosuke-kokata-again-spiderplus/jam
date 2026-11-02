"""Devin CLI harness adapter.

Devin has no daemon-managed background stack: a session is a row in its SQLite
store (`sessions.db`) that a `devin -p` process creates and then runs, and it
stays resumable after the process exits. `spawn` launches `devin -p` detached
and discovers the new id by diffing `devin list` output; `list` uses the CLI
for a directory filter and the read-only database for a global one; `stop`
ends the recorded process (the spawn left a pidfile) and removes the row.

Messaging is out of scope here, as with every adapter: on Devin the primitive
is `devin -r <id> -p "<message>"`, a synchronous resume-and-answer that the
delegate skill names.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Optional

from ..errors import (
    HarnessError,
    InvalidRequest,
    StillListed,
    WorkspaceNotTrusted,
)
from ..session import NO_OPTIONS, Session, SpawnOptions, Spawned, is_id, is_model
from .claude import Runner, run_subprocess

SPAWN_VISIBLE: Final = 20.0  # bound for the new session to appear in `devin list`
POLL: Final = 0.4
_LOG_TAIL: Final = 4096

_NOT_TRUSTED: Final = re.compile(r"untrusted\s+workspace", re.IGNORECASE)
_MODE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]{0,31}")

DEFAULT_DB: Final = "~/.local/share/devin/cli/sessions.db"
_QUERY: Final = (
    "SELECT id, working_directory, backend_type, created_at, title"
    " FROM sessions{filter} ORDER BY created_at DESC"
)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    """True only for a string that matches in full; any other type simply does not match."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _spawn_detached(args: Sequence[str], cwd: str) -> tuple[subprocess.Popen[bytes], str]:
    """Launch `args` detached, output to a temp log; return (process, log path)."""
    fd, log = tempfile.mkstemp(prefix="jam-devin-", suffix=".log")
    with os.fdopen(fd, "ab") as sink:
        proc = subprocess.Popen(
            list(args),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return proc, log


def _chosen_model(options: object) -> Optional[str]:
    """The model to pass as `devin --model`, or None; InvalidRequest for a choice this harness cannot take."""
    if not isinstance(options, SpawnOptions):
        raise InvalidRequest("invalid spawn options")
    if options.effort is not None:
        raise InvalidRequest("this harness has no per-session effort; pass no effort")
    if options.subagent_model is not None:
        raise InvalidRequest("this harness has no subagent model; pass no subagent model")
    if options.model is not None and not is_model(options.model):
        raise InvalidRequest(f"invalid model: {options.model!r}")
    return options.model


# (args, cwd) -> (process-with-pid/poll, log path); the seam that keeps tests from starting sessions.
Spawner = Callable[[Sequence[str], str], "tuple[Any, str]"]


class DevinHarness:
    names_sessions: Final = False  # Devin titles a session itself, so a name is refused and no prefix is made (D19)

    def __init__(
        self,
        devin_bin: str = "devin",
        runner: Optional[Runner] = None,
        spawner: Optional[Spawner] = None,
        db_path: Optional[str] = None,
        spawn_visible: float = SPAWN_VISIBLE,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._bin = devin_bin
        self._run: Runner = runner or run_subprocess
        self._spawn = spawner or _spawn_detached
        self._db = Path(db_path).expanduser() if db_path is not None else Path(DEFAULT_DB).expanduser()
        self._visible = spawn_visible
        self._sleeper = sleeper

    def spawn(
        self,
        cwd: str | Path,
        prompt: str,
        name: str | None = None,
        permission_mode: str | None = None,
        options: SpawnOptions = NO_OPTIONS,
    ) -> Spawned:
        """Start a session in `cwd`: `devin -p` detached, id found via `devin list`.

        A chosen model maps to `devin --model`. Devin has no per-session effort and
        no subagent model, so either is refused, as a name is (D18).
        """
        if not isinstance(cwd, (str, os.PathLike)) or not Path(cwd).is_dir():
            raise InvalidRequest(f"not a directory: {cwd!r}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise InvalidRequest("prompt must not be empty")
        if "\x00" in prompt:
            raise InvalidRequest("prompt must not contain a NUL character")
        if name is not None:
            raise InvalidRequest("this harness names sessions itself; pass no name")
        if permission_mode is not None and not _matches(_MODE, permission_mode):
            raise InvalidRequest(f"invalid permission mode: {permission_mode!r}")
        model = _chosen_model(options)

        target = str(Path(cwd).resolve())
        before = self._listed_ids(target)
        args = [
            self._bin,
            *(["--permission-mode", permission_mode] if permission_mode is not None else []),
            *(["--model", model] if model is not None else []),
            "-p",
            prompt,
        ]
        proc, log = self._spawn(args, target)
        session_id = self._await_new(before, target, proc, log)
        self._write_pid(session_id, proc.pid)
        return Spawned(id=session_id, name=None)

    def list(self, include_exited: bool = False, cwd: str | None = None) -> list[Session]:
        """List sessions: `devin list` under a directory filter, the session store without one."""
        if cwd is not None and (not isinstance(cwd, str) or "\x00" in cwd):
            raise InvalidRequest(f"invalid cwd filter: {cwd!r}")
        return self._cli_rows(cwd) if cwd is not None else self._db_rows(include_exited)

    def stop(self, session_id: str) -> Session | None:
        """End the recorded process if one is still alive, then remove the session.

        Devin has no "stopped but listed" state: the row IS the session, so
        nothing is left to show afterwards and the return is None.
        """
        session_id = self._valid_id(session_id)
        self._kill_tracked(session_id)
        result = self._run([self._bin, "rm", session_id], None)
        if result.returncode != 0:
            raise HarnessError(f"stopping {session_id} failed (exit {result.returncode}): {result.stderr.strip()}")
        self._drop_pid(session_id)
        return self._db_row(session_id)

    def remove(self, session_id: str) -> None:
        """Delete a session record, then check it is gone from the store."""
        session_id = self._valid_id(session_id)
        result = self._run([self._bin, "rm", session_id], None)
        if result.returncode != 0:
            raise HarnessError(f"removing {session_id} failed (exit {result.returncode}): {result.stderr.strip()}")
        self._drop_pid(session_id)
        if self._db_row(session_id) is not None:
            raise StillListed(session_id)

    def _cli_rows(self, cwd: str) -> list[Session]:
        """Sessions in `cwd`, as `devin list --format json` run there reports them."""
        result = self._run([self._bin, "list", "--format", "json"], cwd)
        if result.returncode != 0:
            raise HarnessError(f"listing sessions failed (exit {result.returncode}): {result.stderr.strip()}")
        try:
            rows = json.loads(result.stdout)
        except ValueError as exc:
            raise HarnessError("listing sessions returned output that is not JSON") from exc
        if not isinstance(rows, list):
            raise HarnessError("listing sessions returned JSON that is not a list")
        sessions = (self._session_from(row) for row in rows if isinstance(row, dict))
        return [session for session in sessions if session.id]

    def _db_rows(self, include_exited: bool) -> list[Session]:
        """Every session in the store, newest first; hidden rows only when asked."""
        query = _QUERY.format(filter="" if include_exited else " WHERE hidden = 0")
        return [self._session_from_db(row) for row in self._query(query)]

    def _db_row(self, session_id: str) -> Session | None:
        rows = self._query(_QUERY.format(filter=" WHERE id = ?"), (session_id,))
        return self._session_from_db(rows[0]) if rows else None

    def _query(self, query: str, params: tuple[str, ...] = ()) -> list[sqlite3.Row]:
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                return list(conn.execute(query, params))
        except sqlite3.Error as exc:
            raise HarnessError(f"cannot read the Devin session store {self._db}: {exc}") from exc

    @staticmethod
    def _session_from(row: dict[str, Any]) -> Session:
        """A `devin list` row; its `id` is both the short and the full session id."""
        session_id = row.get("id")
        return Session(
            id=session_id if isinstance(session_id, str) else "",
            session_id=session_id if isinstance(session_id, str) else "",
            name=row["title"] if isinstance(row.get("title"), str) else "",
            cwd=row["working_directory"] if isinstance(row.get("working_directory"), str) else "",
            kind="cli",
            state="resumable",
        )

    @staticmethod
    def _session_from_db(row: sqlite3.Row) -> Session:
        started = row["created_at"]
        return Session(
            id=str(row["id"]),
            session_id=str(row["id"]),
            name=row["title"] if isinstance(row["title"], str) else "",
            cwd=row["working_directory"] if isinstance(row["working_directory"], str) else "",
            kind=row["backend_type"] if isinstance(row["backend_type"], str) else "local",
            state="resumable",
            started_at=started if isinstance(started, int) and not isinstance(started, bool) else None,
        )

    def _listed_ids(self, cwd: str) -> set[str]:
        try:
            return {s.id for s in self._cli_rows(cwd)}
        except HarnessError:
            return set()

    def _await_new(self, before: set[str], cwd: str, proc: Any, log: str) -> str:
        """The id the spawn created, polling `devin list`; failure paths are bounded."""
        deadline = time.monotonic() + self._visible
        while time.monotonic() < deadline:
            new = self._listed_ids(cwd) - before
            if new:
                return self._latest(cwd, new) or sorted(new)[0]
            if proc.poll() is not None:
                self._raise_spawn_failure(log, cwd, proc.poll())
            self._sleeper(POLL)
        raise HarnessError(
            f"the session did not appear in `devin list` within {self._visible:g}s; "
            f"run `devin list` in {cwd} to check before retrying"
        )

    def _latest(self, cwd: str, ids: set[str]) -> str | None:
        """The newest id among `ids`, by the listing's own activity timestamp."""
        try:
            rows = json.loads(self._run([self._bin, "list", "--format", "json"], cwd).stdout)
        except ValueError:
            return None
        ranked = sorted(
            (row for row in rows if isinstance(row, dict) and row.get("id") in ids),
            key=lambda row: row.get("last_activity_at") or 0,
        )
        return ranked[-1]["id"] if ranked else None

    def _raise_spawn_failure(self, log: str, cwd: str, code: object) -> None:
        tail = self._log_tail(log)
        if _NOT_TRUSTED.search(tail):
            raise WorkspaceNotTrusted(cwd)
        raise HarnessError(f"starting a session failed (exit {code}): {tail.strip() or 'no output'}")

    @staticmethod
    def _log_tail(log: str) -> str:
        """The last bytes of the spawn log, then the file is removed; "" when unreadable."""
        with contextlib.suppress(OSError):  # a log that cannot be read just has no diagnostics
            with open(log, "rb") as sink:
                sink.seek(0, 2)
                size = sink.tell()
                sink.seek(max(0, size - _LOG_TAIL))
                tail = sink.read().decode("utf-8", "replace")
            os.unlink(log)
            return tail
        return ""

    def _pidfile(self, session_id: str) -> Path:
        return self._db.parent / "jam-pids" / session_id

    def _write_pid(self, session_id: str, pid: int) -> None:
        with contextlib.suppress(OSError):  # a missing pidfile only means stop cannot kill
            self._pidfile(session_id).parent.mkdir(parents=True, exist_ok=True)
            self._pidfile(session_id).write_text(str(pid))

    def _drop_pid(self, session_id: str) -> None:
        with contextlib.suppress(OSError):
            self._pidfile(session_id).unlink()

    def _kill_tracked(self, session_id: str) -> None:
        """SIGTERM the recorded `devin -p` process when it still is one; never fatal."""
        try:
            pid = int(self._pidfile(session_id).read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return
        probe = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if probe.returncode == 0 and "devin" in probe.stdout:
            with contextlib.suppress(OSError):  # the process may have exited between probe and kill
                os.kill(pid, signal.SIGTERM)

    @staticmethod
    def _valid_id(session_id: str) -> str:
        if not is_id(session_id):
            raise InvalidRequest(f"invalid session id: {session_id!r}")
        return session_id
