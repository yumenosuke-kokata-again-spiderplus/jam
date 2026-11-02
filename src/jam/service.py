"""The service that composes a harness with the registry and the spawn tickets. Stdlib only.

The harness decides which sessions exist; the registry only annotates them. A
registry failure therefore never hides a harness result: after a spawn the
session id is always returned, and a listing degrades to un-annotated rows. The
spawn ticket follows the same rule: a ticket that cannot be written is reported
next to the result and never stops the spawn.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from .errors import DiscardRequired, HarnessError, InvalidRequest, RegistryError
from .naming import prefixed, project_label
from .registry import Record, Registry, spawned_marked
from .session import NO_DISCARD, NO_OPTIONS, Identity, Session, SpawnOptions, Spawned, is_discard_value


class Harness(Protocol):
    names_sessions: bool  # whether `spawn` takes a name; only then does the name get its project prefix (D19)
    discards_unpushed: bool  # whether `remove` takes the value a refusal over unpushed work names (D17)

    def spawn(
        self,
        cwd: str | Path,
        prompt: str,
        name: str | None = None,
        permission_mode: str | None = None,
        options: SpawnOptions = NO_OPTIONS,
    ) -> Spawned: ...

    def list(self, include_exited: bool = False, cwd: str | None = None) -> list[Session]: ...

    def stop(self, session_id: str) -> Session | None: ...

    def remove(self, session_id: str, discard_unpushed: str | None = None) -> None: ...


class TicketWriter(Protocol):
    def issue(self, cwd: str | Path) -> str | None: ...

    def withdraw(self, ticket: str) -> None: ...


class NoTickets:
    """The ticket writer of a Jam that is given no ticket directory: it writes nothing and withdraws nothing."""

    def issue(self, cwd: str | Path) -> str | None:
        return None

    def withdraw(self, ticket: str) -> None:
        return None


NO_TICKETS: Final = NoTickets()


@dataclass(frozen=True)
class SpawnResult:
    spawned: Spawned
    problem: str | None  # set when the session started but jam could not note it or mark it


@dataclass(frozen=True)
class Listed:
    session: Session
    record: Record | None  # None means "unknown", not "not spawned by jam"


@dataclass(frozen=True)
class Listing:
    sessions: tuple[Listed, ...]
    problem: str | None  # why the registry could not be used; harness failures raise instead


def joined(sessions: Sequence[Session], records: Mapping[str, Record | None]) -> tuple[Listed, ...]:
    """Harness rows in harness order, each with its note if one exists. Notes without a row are dropped."""
    return tuple(Listed(session, records.get(session.id)) for session in sessions)


# Leads a close failure that came after the stop, so that clipping a long harness message cannot cut it off. It claims only that the removal step failed:
# the session is stopped, and whether it is still listed is for `list` to say.
STOPPED_NOT_REMOVED: Final = "the session was stopped, but removing it failed: "
OWN_SESSION: Final = "refusing to close the caller's own session (as whoami names it); only its owner may end it that way"


def _after_stop(exc: HarnessError) -> HarnessError:
    """A removal failure led by the note that the session was stopped, keeping the value that discards its work if one was named."""
    message = f"{STOPPED_NOT_REMOVED}{exc}"
    return DiscardRequired(message, exc.token) if isinstance(exc, DiscardRequired) else HarnessError(message)


def failure_reason(exc: Exception) -> str:
    """Only a RegistryError may carry its message; any other message could hold a path."""
    return str(exc) if isinstance(exc, RegistryError) else type(exc).__name__


class Jam:
    def __init__(self, harness: Harness, registry: Registry, tickets: TicketWriter = NO_TICKETS):
        self._harness = harness
        self._registry = registry
        self._tickets = tickets

    def spawn(
        self,
        cwd: str | Path,
        prompt: str,
        name: str | None = None,
        permission_mode: str | None = None,
        options: SpawnOptions = NO_OPTIONS,
    ) -> SpawnResult:
        """Write the spawn ticket, start a session, then note that jam started it.

        A harness that names sessions gets the name with the project in front (D19).
        The ticket exists before the harness runs, so the session's first start can
        claim it, and it is withdrawn when the harness raises. Harness failures
        propagate and nothing is noted. Once the session exists, no registry or
        ticket failure may hide its id, and the registry lock is never held while
        the harness runs.
        """
        if self._harness.names_sessions:
            name = prefixed(name, project_label(cwd))
        ticket, ticket_error = self._issued(cwd)
        try:
            spawned = self._harness.spawn(cwd, prompt, name, permission_mode, options)
        except BaseException:
            self._withdrawn(ticket)
            raise
        return SpawnResult(spawned, _joined_problems(_ticket_problem(ticket_error, spawned.id), self._recorded(spawned.id)))

    def list(self, include_exited: bool = False, cwd: str | None = None) -> Listing:
        sessions = self._harness.list(include_exited, cwd)
        try:
            records = {session.id: self._registry.record(session.id) for session in sessions}
        except RegistryError as exc:
            return Listing(joined(sessions, {}), str(exc))
        return Listing(joined(sessions, records), None)

    def stop(self, session_id: str) -> Session | None:
        # A stopped session stays in the full listing, so its note stays true.
        return self._harness.stop(session_id)

    def close(self, session_id: str, discard_unpushed: str | None = None, caller: Identity | None = None) -> None:
        """Stop the session, then delete it unless stopping already did; the harness decides what a delete refuses.

        The one place that deletes a session, and only on the owner's say-so
        (the tool description and the skill carry that). The caller's own
        session is refused before anything runs; a caller the harness cannot
        name has no session here. jam forces no refusal: `discard_unpushed` is
        only the value an earlier refusal named, passed on as given. Stopping
        comes first for every session: it ends a run that is still going, it
        is harmless on one that is stopped but still listed (observed, D17),
        and on the Devin CLI it is the deletion itself, which leaves no row to
        remove. A failure of the removal comes back saying the session was
        stopped, because a refusal leaves it stopped and still listed.
        """
        if discard_unpushed is not None and not self._harness.discards_unpushed:
            raise InvalidRequest(NO_DISCARD)
        if discard_unpushed is not None and not is_discard_value(discard_unpushed):
            raise InvalidRequest(f"invalid discard_unpushed value: {discard_unpushed!r}")
        if caller is not None and self._callers_own(session_id, caller):
            raise InvalidRequest(OWN_SESSION)
        if self._harness.stop(session_id) is None:
            return
        try:
            self._harness.remove(session_id, discard_unpushed)
        except HarnessError as exc:
            raise _after_stop(exc) from exc

    def _callers_own(self, session_id: str, caller: Identity) -> bool:
        """Whether `session_id` is the caller's session: by its short id, or, when that is unknown (after /clear), by the row of its full id."""
        if caller.id is not None:
            return caller.id == session_id
        return any(row.id == session_id and row.session_id == caller.session_id for row in self._harness.list(include_exited=True))

    def _issued(self, cwd: str | Path) -> tuple[str | None, Exception | None]:
        """The ticket written for `cwd` and None, or None and the error that kept it from being written. Never raises."""
        try:
            return self._tickets.issue(cwd), None
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad: a ticket that cannot be written must not stop the spawn. The error is kept for the problem text, which names only its type unless it is a RegistryError.
            return None, exc

    def _withdrawn(self, ticket: str | None) -> None:
        """Remove the ticket of a spawn that did not start. A failure to do so is ignored: the harness error is the one the caller needs."""
        if ticket is None:
            return
        with contextlib.suppress(Exception):  # noqa: BLE001 - the harness error is raised instead
            self._tickets.withdraw(ticket)

    def _recorded(self, session_id: str) -> str | None:
        try:
            self._registry.update(session_id, spawned_marked)
        except Exception as exc:  # noqa: BLE001
            # Deliberately broad, and only here: the worker already exists and the
            # return value is the only way its id reaches the caller. A bug in the
            # registry must not turn a successful spawn into an exception, because
            # the caller would then respawn. The exception type is kept in the text.
            return f"could not record {session_id}: {failure_reason(exc)}"
        return None


def _ticket_problem(error: Exception | None, session_id: str) -> str | None:
    """The problem for a ticket that could not be written, named by the session id that is known only after the spawn; None when it was written."""
    if error is None:
        return None
    return f"could not mark {session_id} as started by jam: {failure_reason(error)}"


def _joined_problems(*problems: str | None) -> str | None:
    """The problems that are there, in the order given and joined by "; ", or None when there are none."""
    found = [problem for problem in problems if problem is not None]
    return "; ".join(found) if found else None
