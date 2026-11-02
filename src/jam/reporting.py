"""How results and failures read to a caller. Pure, and shared by both transports.

A result is one JSON object on one line. A failure is one clean line of text:
harness stderr is multi-line, may carry ANSI escapes, and is echoed raw by the
harness, so every failure text is flattened and capped before it leaves.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Optional

from .errors import DiscardRequired, HarnessError, InvalidRequest, RegistryError, UnparseableOutput, WorkspaceNotTrusted
from .service import Listed, Listing, SpawnResult
from .session import Identity, Session

MAX_FAILURE_CHARS: Final = 2000
MAX_RAW_CHARS: Final = 200
NOT_STARTED: Final = " | nothing was started; do not retry until the owner confirms"
MAYBE_STARTED: Final = " | a session may have started: run list before retrying"
# Ends a refusal over unpushed work (D17); kept apart from the message so that clipping never cuts the value.
DISCARDABLE: Final = (
    " | close again with discard_unpushed {token} (--discard-unpushed on the command line) to delete that work too,"
    " only if that commit is the head of a merged pull request or the owner said to drop it"
)
_ANSI: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")


@dataclass(frozen=True)
class Stopped:
    """What `stop` reports: the id it was given and the row the harness shows afterwards, if any."""

    id: str
    session: Optional[Session]


@dataclass(frozen=True)
class Closed:
    """What `close` reports: the id it was given. A failure raises instead."""

    id: str


def session_row(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "session_id": session.session_id,
        "name": session.name,
        "cwd": session.cwd,
        "kind": session.kind,
        "state": session.state,
        "status": session.status,
    }


def listed_row(listed: Listed) -> dict[str, Any]:
    """`spawned` is None, never False, when jam has no note: no note means unknown."""
    spawned = None if listed.record is None else listed.record.spawned
    return {**session_row(listed.session), "spawned": spawned}


def spawn_object(result: SpawnResult) -> dict[str, Any]:
    return {"id": result.spawned.id, "name": result.spawned.name, "problem": result.problem}


def listing_object(listing: Listing) -> dict[str, Any]:
    return {"sessions": [listed_row(listed) for listed in listing.sessions], "problem": listing.problem}


def stopped_object(stopped: Stopped) -> dict[str, Any]:
    return {"id": stopped.id, "session": None if stopped.session is None else session_row(stopped.session)}


def closed_object(closed: Closed) -> dict[str, Any]:
    return {"id": closed.id, "closed": True}


def identity_object(identity: Identity) -> dict[str, Any]:
    return {"session_id": identity.session_id, "id": identity.id}


_OBJECTS: Final = {
    SpawnResult: spawn_object,
    Listing: listing_object,
    Session: session_row,
    Stopped: stopped_object,
    Closed: closed_object,
    Identity: identity_object,
}


def rendered(result: object) -> str:
    """The result as one line of JSON, with real characters rather than escapes."""
    return json.dumps(_OBJECTS[type(result)](result), ensure_ascii=False)


def flattened(text: object) -> str:
    """One line: ANSI escapes and control characters gone, every whitespace run a single space."""
    plain = _ANSI.sub("", str(text))
    kept = "".join(" " if char.isspace() else "" if unicodedata.category(char) == "Cc" else char for char in plain)
    return " ".join(kept.split())


def clipped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def described(exc: Exception) -> tuple[str, str]:
    """The message for one failure, and the note on whether a session started despite it.

    The note is kept apart from the message because it is the part that must
    survive clipping. The order matters: InvalidRequest is a HarnessError, and so
    are the two after it.
    """
    if isinstance(exc, InvalidRequest):
        return str(exc), ""
    if isinstance(exc, DiscardRequired):
        return str(exc), DISCARDABLE.format(token=exc.token)
    if isinstance(exc, WorkspaceNotTrusted):
        return str(exc), NOT_STARTED
    if isinstance(exc, UnparseableOutput):
        return f"{exc} | output: {flattened(exc.raw)[:MAX_RAW_CHARS]}", MAYBE_STARTED
    if isinstance(exc, (HarnessError, RegistryError)):
        return str(exc), MAYBE_STARTED
    return f"internal error: {type(exc).__name__}", MAYBE_STARTED


def failure_text(operation: Any, exc: Exception) -> str:
    """The one-line text for a failed operation; `operation` only needs a `name`.

    A spawn that failed after it could have started a session also says so, so a
    caller lists before it retries. Whether a session started is the last thing
    cut from a long message.
    """
    message, note = described(exc)
    tail = "" if note == MAYBE_STARTED and operation.name != "spawn" else note
    return clipped(flattened(message), MAX_FAILURE_CHARS - len(tail)) + tail
