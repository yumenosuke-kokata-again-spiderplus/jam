"""Session names that carry their project, so a list of sessions from several projects says which is which. Stdlib only.

The project is named by a directory: the main checkout of the repository the
spawn directory belongs to (a linked worktree names its main checkout, not
itself), else the spawn directory. Its name is cut down to the characters a
session name allows. Reading it never fails: an unknown project is no label
(D19).
"""

from __future__ import annotations

import os
import re
from typing import Final, Optional

from .repos import repo_of
from .session import ID_PATTERN

MAX_NAME: Final = 64  # the length ID_PATTERN allows
SEPARATOR: Final = "-"
_OUTSIDE: Final = re.compile(r"[^A-Za-z0-9._-]+")
_EDGES: Final = "._-"  # a name starts with a letter or a digit, and a label ends with one


def project_directory(cwd: str) -> str:
    """The directory that names the project of the absolute `cwd`: its repository's main checkout, else `cwd` itself."""
    repo = repo_of(cwd)
    if repo is None:
        return cwd
    common = repo.identity
    return os.path.dirname(common) if os.path.basename(common) == ".git" else repo.root


def project_label(cwd: object) -> Optional[str]:
    """The project's directory name in the characters a session name allows, or None when nothing of it is left."""
    if not isinstance(cwd, (str, os.PathLike)):
        return None
    path = os.fspath(cwd)
    if not isinstance(path, str) or "\0" in path or not os.path.isabs(path):
        return None
    base = os.path.basename(os.path.normpath(project_directory(path)))
    return _OUTSIDE.sub(SEPARATOR, base).strip(_EDGES) or None


def prefixed(name: Optional[str], label: Optional[str]) -> Optional[str]:
    """`name` led by `label` and a dash, unless no label is known or `name` already starts with it.

    No name stays no name, so the harness keeps its own title for the session,
    and an invalid name is left for the harness to refuse. The caller's part is
    never cut: the label is shortened to fit the length limit, and left off when
    none of it fits.
    """
    if name is None or label is None or ID_PATTERN.fullmatch(name) is None:
        return name
    folded = name.casefold()
    if folded == label.casefold() or folded.startswith(f"{label}{SEPARATOR}".casefold()):
        return name
    head = label[: max(MAX_NAME - len(name) - len(SEPARATOR), 0)].rstrip(_EDGES)
    return f"{head}{SEPARATOR}{name}" if head else name
