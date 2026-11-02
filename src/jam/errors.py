"""Errors shared by the core and the adapters. Stdlib only."""

from __future__ import annotations


class HarnessError(Exception):
    """The harness failed or answered in a way jam cannot use."""


class InvalidRequest(HarnessError, ValueError):
    """The caller's arguments were rejected before anything was run."""


class WorkspaceNotTrusted(HarnessError):
    """The harness refuses to start a session in a directory the owner has not trusted."""

    def __init__(self, path: str):
        super().__init__(
            f"workspace not trusted: run the agent once in {path}, accept the trust prompt, then retry"
        )
        self.path = path


class UnparseableOutput(HarnessError):
    """The command succeeded but its output was not in a recognised shape.

    A session may exist even so, so the raw output is kept for the caller.
    """

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


class RegistryError(Exception):
    """The session registry could not be read or written, or its lock timed out.

    Not a HarnessError on purpose: a registry failure must never be mistaken for a
    harness failure, because the registry is only an annotation layer.
    """


class DiscardRequired(HarnessError):
    """The harness kept a session over work that exists nowhere else; `token` is the value that deletes it anyway (D17).

    The token is kept apart from the message, so that clipping a long refusal cannot cut it.
    """

    def __init__(self, message: str, token: str):
        super().__init__(message)
        self.token = token


class StillListed(HarnessError):
    """A session that was removed still shows up in the harness listing."""

    def __init__(self, session_id: str):
        super().__init__(f"session {session_id} is still listed after removal")
        self.session_id = session_id
