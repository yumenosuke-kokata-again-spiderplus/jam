"""Claude Code harness adapter.

Wraps the native background-session commands (`claude --bg`, `claude agents
--json`, `claude stop`, `claude rm`) instead of replacing them. Every call goes
through an injectable runner so tests never start a real session.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional

from ..errors import (
    HarnessError,
    InvalidRequest,
    StillListed,
    UnparseableOutput,
    WorkspaceNotTrusted,
)
from ..session import Session, Spawned

DEFAULT_TIMEOUT: Final = 60.0
# Where a command that needs no directory runs. A child otherwise inherits this process's
# directory, and `claude` and `devin` both refuse to run once that was deleted (a worktree
# cleaned up under a running MCP server); the root always exists (D12).
NEUTRAL_DIR: Final = "/"

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SPAWNED: Final = re.compile(r"^backgrounded\s*·\s*([0-9a-f]{8})(?:\s*·\s*(\S.*?))?\s*$", re.MULTILINE)
_NOT_TRUSTED: Final = re.compile(r"not\s+trusted", re.IGNORECASE)
_TRUST_PATH: Final = re.compile(r"\bin\s+(.+?)\s+once\b")
# Names and ids become argv entries, so a leading "-" would be read as a flag.
_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MODE: Final = re.compile(r"[A-Za-z][A-Za-z0-9]{0,31}")
# `claude stop` and `claude rm` match the start of an id, so a prefix would stop one session and then look gone; only the whole short id is passed on.
_SHORT_ID: Final = re.compile(r"[0-9a-f]{8}")
# `claude rm` reports a refusal on stdout, and the same text names the commands that override it (read from the strings of the 2.1.309 binary, D17; no live refusal was seen).
# Those lines are dropped: jam never forces a removal and must not hand the way to the model.
_OVERRIDES: Final = ("--discard-unpushed", "--force-remove-worktree", "ctrl+x", "claude rm ")


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    """True only for a string that matches in full; any other type simply does not match."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


# Evaluated at import time, so this alias stays on `Optional` for Python 3.9.
Runner = Callable[[list[str], Optional[str]], RunResult]


def run_subprocess(args: Sequence[str], cwd: str | None, timeout: float = DEFAULT_TIMEOUT) -> RunResult:
    """Run a command with stdin closed and a timeout, without a shell, in `cwd` or else the root.

    Arguments (a prompt, for one) are never echoed into error messages.
    """
    try:
        proc = subprocess.run(
            list(args),
            cwd=NEUTRAL_DIR if cwd is None else cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(f"{args[0]!r} timed out after {timeout:g}s") from exc
    except OSError as exc:  # not found, not executable, or a directory on PATH is unreadable
        reason = "executable not found" if isinstance(exc, FileNotFoundError) else (exc.strerror or "OS error")
        raise HarnessError(f"cannot run {args[0]!r}: {reason}") from exc
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


class ClaudeHarness:
    def __init__(self, claude_bin: str = "claude", runner: Runner | None = None):
        self._bin = claude_bin
        self._run: Runner = runner or run_subprocess

    def spawn(
        self,
        cwd: str | Path,
        prompt: str,
        name: str | None = None,
        permission_mode: str | None = None,
    ) -> Spawned:
        """Start a background session in `cwd` with `prompt` as its first message."""
        if not isinstance(cwd, (str, os.PathLike)) or not Path(cwd).is_dir():
            raise InvalidRequest(f"not a directory: {cwd!r}")
        directory = Path(cwd)
        if not isinstance(prompt, str) or not prompt.strip():
            raise InvalidRequest("prompt must not be empty")
        if "\x00" in prompt:
            raise InvalidRequest("prompt must not contain a NUL character")
        if name is not None and not _matches(_NAME, name):
            raise InvalidRequest(f"invalid session name: {name!r}")
        if permission_mode is not None and not _matches(_MODE, permission_mode):
            raise InvalidRequest(f"invalid permission mode: {permission_mode!r}")

        args = [
            self._bin,
            "--bg",
            *(["-n", name] if name is not None else []),
            *(["--permission-mode", permission_mode] if permission_mode is not None else []),
            "--",
            prompt,
        ]
        target = str(directory.resolve())
        result = self._run(args, target)
        if result.returncode != 0:
            self._raise_for_spawn_failure(result, target)
        return self._spawned_from(result.stdout)

    def list(self, include_exited: bool = False, cwd: str | None = None) -> list[Session]:
        """List sessions. Rows without a usable id are skipped; unknown keys are ignored."""
        if cwd is not None and (not isinstance(cwd, str) or "\x00" in cwd):
            raise InvalidRequest(f"invalid cwd filter: {cwd!r}")
        args = [
            self._bin,
            "agents",
            "--json",
            *(["--all"] if include_exited else []),
            *(["--cwd", cwd] if cwd is not None else []),
        ]
        result = self._run(args, None)
        if result.returncode != 0:
            raise HarnessError(f"listing sessions failed (exit {result.returncode}): {result.stderr.strip()}")
        try:
            rows = json.loads(result.stdout)
        except ValueError as exc:
            raise HarnessError("listing sessions returned output that is not JSON") from exc
        if not isinstance(rows, list):
            raise HarnessError("listing sessions returned JSON that is not a list")
        sessions = (Session.from_agent(row) for row in rows if isinstance(row, dict))
        return [session for session in sessions if session is not None]

    def stop(self, session_id: str) -> Session | None:
        """Stop a session and report the row the harness shows afterwards, if any.

        A stopped session leaves the plain listing at once but stays in the full
        listing, without a status or pid, until it is removed. Its state is passed
        through as shown: "stopped" was observed, and so was "done" for a session
        that had already finished its work.
        """
        session_id = self._valid_id(session_id)
        result = self._run([self._bin, "stop", session_id], None)
        if result.returncode != 0:
            raise HarnessError(f"stopping {session_id} failed (exit {result.returncode}): {result.stderr.strip()}")
        return self._listed(session_id)

    def remove(self, session_id: str) -> None:
        """Delete a session, then check that it is gone from the full listing."""
        session_id = self._valid_id(session_id)
        result = self._run([self._bin, "rm", session_id], None)
        if result.returncode != 0:
            raise HarnessError(self._removal_failure(session_id, result))
        if self._listed(session_id) is not None:
            raise StillListed(session_id)

    def _listed(self, session_id: str) -> Session | None:
        return next((s for s in self.list(include_exited=True) if s.id == session_id), None)

    @staticmethod
    def _removal_failure(session_id: str, result: RunResult) -> str:
        shown = (_ANSI.sub("", line).strip() for line in result.stdout.splitlines())
        reasons = " ".join(line for line in shown if line and not any(token in line.lower() for token in _OVERRIDES))
        detail = " | ".join(part for part in (result.stderr.strip(), reasons) if part)
        return f"removing {session_id} failed (exit {result.returncode})" + (f": {detail}" if detail else "")

    @staticmethod
    def _valid_id(session_id: str) -> str:
        if not _matches(_SHORT_ID, session_id):
            raise InvalidRequest(f"invalid session id: {session_id!r}")
        return session_id

    @staticmethod
    def _raise_for_spawn_failure(result: RunResult, requested_dir: str) -> None:
        message = next((text for text in (result.stderr, result.stdout) if _NOT_TRUSTED.search(text)), None)
        if message is not None:
            found = _TRUST_PATH.search(_ANSI.sub("", message))
            raise WorkspaceNotTrusted(found.group(1) if found else requested_dir)
        raise HarnessError(f"starting a session failed (exit {result.returncode}): {result.stderr.strip()}")

    @staticmethod
    def _spawned_from(stdout: str) -> Spawned:
        found = _SPAWNED.search(_ANSI.sub("", stdout))
        if found is None:
            raise UnparseableOutput("could not find the new session id in the command output", stdout)
        return Spawned(id=found.group(1), name=found.group(2))
