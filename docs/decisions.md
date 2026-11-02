# Decisions

Newest information wins; each entry says what was decided, why, and what was
rejected. Items marked **unverified** are assumptions to confirm before relying
on them.

## D1. Build on the native background-session stack

Decided: wrap Claude Code's own background sessions instead of writing a socket
daemon.

Verified on Claude Code 2.1.284: `claude --bg` starts a background session,
`claude agents --json` lists live sessions (id, cwd, kind, status), and
`claude logs|attach|stop|rm <id>` manage them. The built-in daemon already owns
each background session's pseudo-terminal and uses Unix sockets internally.

Rejected: a custom socket daemon. It would duplicate the native one and depend
on its own protocol staying in sync.

## D2. Messaging uses the native `SendMessage`

Revised on 2026-10-15; the original design is kept below as the fallback.

Decided: sessions talk to each other through Claude Code's own `SendMessage`
instead of a jam-owned transport. Verified by experiment between this
orchestrating session and a `claude --bg` worker:

- parent to an idle worker: the message wakes the worker;
- worker to a running parent: the message arrives mid-turn as a
  `<cross-session-message>`;
- worker to an idle parent: the message wakes the parent.

Delivery goes through a per-session Unix socket that the harness manages. The
`SendMessage` description says that a session in a different permission mode
holds a message for its user's approval; between background sessions this was
not seen for any mode tried (see "Cross-session messages and permission modes"
under "Plugin platform facts"). The roles above (parent, worker) only describe
who started whom in the experiment; see D10.

**Caveat.** In the experiment the receiving parent was itself a
`kind: background` session. A foreground terminal session was tried later, and
a worker's message woke it (it ran in a tmux pane; see "Cross-session messages
and permission modes" under "Plugin platform facts"); a plain terminal and a
zellij pane were not tried. The owner had decided that whether one can be woken
does not matter: jam only needs participating sessions to be registered with
Claude Code's background session stack (see D4).

Original design, now the fallback: delivering a prompt to a live session means
writing to its pseudo-terminal, and reading a result means reading its screen.
Screen text is messy, so results would travel through a file mailbox instead.

| Terminal owner | Write | Read |
|---|---|---|
| Claude Code daemon (background sessions) | attach / daemon pty socket | `claude logs` |
| zellij (interactive sessions) | `zellij action write-chars --pane-id` | `zellij action dump-screen --pane-id` |

## D3. Hook delivery by session state is deferred

Superseded by D2: the native messaging already covers a busy, a mid-turn, and
an idle receiver among sessions registered with the background stack, so jam
does not build a mailbox or the hook delivery below in v1. This table would
return only if native messaging proves insufficient for a registered session.

| Session state | Delivery |
|---|---|
| Busy | PostToolUse hook reads the mailbox and injects it as `additionalContext` |
| About to stop | Stop hook reads the mailbox and continues with `decision: block` |
| Idle | Hooks do not run; wake it by writing to its zellij pane (**unverified**: whether a trailing carriage return submits the prompt) |

## D4. Scope: Claude Code first, behind adapter seams

Decided on 2026-10-15 (native-first).

Core is a session registry plus the verbs `spawn`, `list`, `stop`, `close`, and
`whoami` (D12 shows how the entry points expose them; `close` is D17). It is stdlib-only and knows
nothing about a harness or a terminal. `send` and `result` are not jam verbs in
v1: the skill (D15) teaches the AI to use the native `SendMessage`, addressing a
session by the name `ListAgents` prints. The harness adapter declares what it
can do in v1 (`spawn_bg`, `list`, `stop`, `remove`); messaging is the native
`SendMessage`, not a jam capability. The environment seam would declare
terminal-specific capabilities (`inject`, `read_screen`, `wake_idle`) only if an
adapter were ever added, and the core degrades gracefully without them.

v1 ships only the Claude Code harness adapter and no environment adapter. jam
does not depend on a terminal multiplexer: it works for sessions that are
registered with Claude Code's background session stack (visible in
`claude agents` with an `id`). A session that is not registered there is out of
scope for v1, and the README states that requirement. A foreground session
shows up there as a row without an `id`, which jam skips; in the one check made,
a worker's message woke one all the same (see "Cross-session messages and
permission modes" under "Plugin platform facts").
The environment seam stays so that a zellij adapter can be added later without
touching the core, but it is not planned. Codex and other harnesses are out of
scope.

## D5. Plugin, installed with nothing else

The repository is its own marketplace. The MCP server, hooks, and skill ship in
one plugin. The AI is told the CLI's absolute path by a SessionStart hook
(`additionalContext`, D13), so no PATH change is needed. In the one session
checked, `/reload-plugins` did not bring the notice to a session that was
already running when the plugin was installed (see "Plugin platform facts"). The
MCP server and the CLI are thin entry points over the same core; their shape is
D12. The registry lives in the plugin data directory and is disposable (see
"Plugin platform facts"). Its shape is D11.

## D6. Automatic delegation guard

A PreToolUse hook on Edit, Write, MultiEdit, and NotebookEdit fires when the
target file is in a repository other than the one the session started in
(worktrees of the same repository, the start directory's own repository, non-git
paths, and gitignored files are excluded; a repository nested below the start
directory is not, see D14-6). It denies once per (session, repository) with a
reason pointing to jam delegation; the re-issued edit passes. Rationale: relying
on the model to remember to use the tool did not work in a comparable
canvas-based setup, while a hook fires on the event itself. Not forced on every
edit, so legitimate direct edits are never blocked.

Why delegate at all: a session started in the other repository loads that
repository's own `.claude` instructions, rules, skills, and hooks into its
context, so the work follows the local conventions. An edit made from outside
follows the setup of the session that makes it, which may not include them.
Orchestrating work across repositories this way is the main benefit of jam, and
the guard exists to make it the default path.

Limits: shell writes (`sed -i`, `git -C`, redirects) are not detected in v1.

Loop protection is required. An environment variable set by the spawner does not
reach the worker (see Platform facts), so a depth counter cannot travel that
way, and D10 rules out relying on spawn-chain depth alone. D14 (accepted on
2026-10-20) settles the mechanism: `spawn` writes a ticket before the worker
starts, the first fresh start in that directory (D13) takes the ticket as the
mark, a marked session is not denied while its mark exists (30 days from the
spawn, see D14), and an hourly limit bounds whatever recognition misses. The two
earlier candidates were a record keyed by the worker's short id, which races
because the worker's hook can fire before the spawner records anything, and a
marker in the spawn prompt, which no hook input carries; D14 records why both
were rejected.

## D7. Name

`jam`, expanded as "Junction for AI Messaging". The name mentions neither a
harness nor a terminal so adapters can be added later.

## D8. MCP tools are approved by the user on first use

Decided on 2026-10-15: jam does not ship a `PermissionRequest` hook that
approves its own MCP tools. The owner approves them once on first use.
Rejected: the auto-allow hook. A plugin granting itself permission is a
self-grant that the user did not make, and the cost it would save is one prompt.

## D9. License

MIT, chosen by the owner on 2026-10-15. The text is in `LICENSE`.

## D10. Sessions are peers

Decided on 2026-10-15. Once a session is started, the session that spawned it
and the spawned session are equals. The owner keeps instructing the spawned
session directly, as with any session, and a spawned session may message any of
its ancestors on its own initiative, not only reply to its spawner.

Consequences: jam models a flat set of peers, not a tree. jam lists sessions
without a hierarchy, and the registry only annotates the harness's rows (the
spawner's id may be kept as provenance only, see D11),
and `whoami` lets any session learn its own id. A message from a peer is input
to weigh, never an instruction that carries the sender's authority. Loop
protection (D6) cannot rely on a spawn-chain depth alone, because a peer can be
addressed by a session that did not spawn it; it is designed with the guard.

## D11. The session registry

Decided on 2026-10-15, from three independent designs reconciled into one.

- **Shape.** One small JSON file per session, `sessions-v1/<id>.json` under the
  plugin data directory, keyed by the harness's short id (the one `spawn`
  returns; the full `sessionId` would cost another harness call at the one point
  that must not fail). The only field jam reads is `spawned`. Unknown keys are
  kept on every write, so an older writer never erases a newer one's fields. A
  format change renames the directory (`sessions-v2`) instead of adding a
  version field.
- **Harness first.** The harness stays the source of truth for which sessions
  exist. `list` is a left join from harness rows onto notes; a note without a
  row is never shown, and a missing or unreadable note means "unknown", not
  "not spawned by jam".
- **Locking.** Only writes lock. `flock` on a sidecar lock file (not the data
  file, whose inode `os.replace` swaps) is polled against a short deadline,
  because `flock` has no timeout and a blocking wait would hang a hook. The
  write goes to a temp file in the same directory and is renamed over the note;
  there is no fsync because the data is disposable. Reads, `remove`, and `prune`
  take no lock, and the lock is never held while the harness spawns.
- **Failure.** Damaged, oversized, or wrongly typed content reads as "no note"
  and the next update overwrites it. An OS error (permissions, I/O) is never
  treated as an absence: it raises `RegistryError`, which is not a
  `HarnessError`, carries no path, and never leads to an overwrite. A spawn that
  succeeded never raises because of the registry: `Jam.spawn` returns the id
  together with a `problem` text, so a caller is not tempted to respawn.
- **Sweeping.** By file age only (30 days), not against the harness listing:
  the foreground session tried appeared in `claude agents` only as a row without
  an `id` (see "Background session commands" under "Plugin platform facts"), yet
  `adopt` wrote a start record for it (D14; the record stayed after the session
  exited, and no worker mark was made because no ticket was waiting), and a
  listing-based sweep could delete the files of live sessions it cannot match.
  The SessionStart hook calls `prune` on `startup` (D13). `remove` exists but no
  hook calls it: SessionEnd registers nothing (D13), because a
  stopped session stays in the full listing and a resumed one can return with
  the same id.
- **Platform.** macOS and Linux (`fcntl`), local filesystem only. The data
  directory and its subdirectory are created 0700 (the notes 0600), whatever the
  umask, so another local user cannot replace them.
- **Links.** `sessions-v1` must be a real directory, not a link: `update`,
  `record`, `remove` and `prune` raise `RegistryError` rather than go through
  one, while a directory that is merely absent is still "no note" and "nothing to
  prune". The data directory above it belongs to the platform and is not checked.
  The check is one `lstat` before the use, and the gap between the two (time of
  check to time of use) is accepted, since only the same user can plant a link in
  the user's own plugin data directory.
- **Not stored, on purpose.** The spawner's id (no reader, and nothing can write
  it yet), the working directory, the full session id, and timestamps (the
  file's modification time is the clock for `prune`). Each is added by the change
  that brings its reader.
- **Not solved here.** D6: `spawned` is recorded after the spawn returns, so a
  worker's first hook can run before the note exists.

## D12. Entry points

Decided on 2026-10-20, from several independent designs reconciled into one and
checked against the official documentation (see "Plugin platform facts" for
which claims are documented and which are client behavior seen in the design
pass).

- **One table, two transports.** `spawn`, `list`, `stop`, `close`, and `whoami`
  are rows of one operations table. The table generates the MCP tool schemas and the CLI
  parser, checks arguments in one place, and is the only code that runs the core
  and the only code that catches exceptions. The MCP server and the CLI are thin
  transports over it, so they cannot disagree: for the same arguments the CLI
  prints the very JSON line that the MCP tool returns as its text. A composition
  root is the only non-test module that names the Claude Code adapter, and one
  module of the harness adapter is the only home of `CLAUDE_*` variable names.
  A later verb costs one table row and its tests.
- **Data directory.** The CLI takes a global `--data-dir DIR` flag that precedes
  the command; the MCP server takes `CLAUDE_PLUGIN_DATA`, which the platform
  exports to it, so the server's declaration passes no data-dir argument. One
  resolver serves both: a non-empty flag, else the variable, else none. The
  SessionStart notice bakes the flag into one copy-paste command prefix (D13); a
  CLI call without the flag runs without a registry (see the degradation below).
  Rejected:
  - the variable alone: it does not reach commands the model runs through the
    Bash tool (see "Plugin platform facts"), so the CLI would have no registry;
  - MCP only, with no CLI: no shell fallback when the tools are unavailable;
  - deriving the documented data directory from the plugin id: a hardcoded
    path, which D5 and the public-repository rule forbid, and it would break
    silently if the platform changed how the directory is named;
  - a must-pre-exist check, or creating the directory only in a hook: it
    contradicts the lazy initialization of D11 and breaks the first session
    after install, before any hook has run.
- **Registry degradation.** A missing, empty, or relative data directory does not
  fail the call. The composition root substitutes a stand-in registry whose
  writes raise `RegistryError` with a path-free reason, which the service turns
  into `problem` next to a result that is still correct (D11): `spawn` still
  returns its id, `list` still returns its rows, and `stop`, `close` and `whoami`
  never touch the registry. Nothing is created at startup or at construction, because
  the platform may start the MCP server more than once per session start.
  An empty listing reads no notes, so it reports no `problem` even when the
  registry is unavailable; the first `spawn` names the reason.

- **Errors: `isError` for the tool, protocol errors for the envelope.** A failure
  inside a known tool is a tool result with `isError: true`, so the model reads
  it and can react; the MCP specification reserves protocol errors for a
  malformed message, an unknown tool, or malformed params. A failure is one
  clean line: whitespace collapsed, escape sequences and control characters
  removed, capped at 2000 characters, never a traceback. Harness stderr is
  multi-line and echoed raw, and a message from an unexpected exception could
  hold a path, so such an exception reports only its type name. A `spawn`
  failure that may have started a session says so and tells the caller to run
  `list` before retrying; a "workspace not trusted" failure says nothing was
  started and not to retry until the owner confirms. The CLI mirrors this: exit
  0 on success, including a result whose `problem` is set (the session exists,
  and a nonzero code would invite a respawn); 1 with one `jam: ...` line on
  stderr and nothing on stdout when the operation failed; 2 for a usage error.
- **`whoami` comes from `CLAUDE_CODE_SESSION_ID`, and over MCP it can be
  stale.** It is a pure function of the environment and makes no harness call:
  `session_id` is that variable (a failure naming the variable, never its value,
  when it is missing or malformed), and `id` is the last path component of
  `CLAUDE_JOB_DIR` when that passes the id check and the session id starts with
  it, else null. The short id is never cut from the first 8 characters of the
  session id: that relation was observed (see "`--session-id` under `--bg`") but
  is not documented, so it only confirms a name the job directory supplies. A
  foreground session started from the shell of a background session inherits
  that session's `CLAUDE_JOB_DIR`, and the check keeps it from reporting the
  other session's short id as its own (#20). The check has a cost. `/clear`
  gives a background session a new session id and its job directory keeps the
  old name (see "Session variables"), so from then on the command run through
  Bash reports `id` as null where it used to report the right one. In the one
  check made (a background session attached in a terminal), the `list` row kept
  its `id` and held the new `sessionId` in the first listing read, about 4
  seconds after the `/clear` was sent (see "Session variables"), so the fallback
  below found the row; the id an MCP server sees after `/clear` was not checked.
  Where no row matches, the `whoami` description tells the model that its short
  id is unknown. A wrong id is worse
  than none: the `stop`
  description relies on it to keep a session from stopping itself, and a peer
  given it could pass it to `stop`. If a later version drops the relation, `id`
  is null everywhere, and the caller falls back to `list` as below. The
  documentation says an MCP server keeps the id it was started with, so after
  `/clear`, or `--continue` and `--resume` without an explicit id, the MCP
  answer may be stale, while the `session_id` from the command run through Bash
  is always current. Because `CLAUDE_JOB_DIR` is documented only for background
  sessions and their shell commands, `id` is null in a foreground session (the
  check above covers one that inherited a job directory) and not guaranteed over
  MCP; the caller then runs `whoami` through Bash and matches its current
  `session_id` in `list`. The foreground session tried (2026-10-28) had no row
  there: `claude agents` listed it without an `id`, which `list` skips, so for it
  the match finds nothing; it has no short id, and `whoami` leaves `id` null.
  Reading the short id from the harness listing inside `whoami` would add a
  harness call and is deferred. Only the two named
  variables are read: another `CLAUDE_CODE_SESSION_*` variable carries a
  credential.
- **`permission_mode` is not exposed.** The core's `spawn` keeps the parameter,
  but neither entry point offers it, and supplying it is an unknown-argument
  error (a tool result with `isError`, or CLI exit 2). A mode chosen by the
  model would bypass the per-tool first-use approval of D8. Without one, a
  worker starts in the owner's `permissions.defaultMode`, and messages between
  background sessions flowed both ways for every mode tried (see "Cross-session
  messages and permission modes" under "Plugin platform facts"), so nothing
  calls for choosing one. If it is ever exposed, use an owner-visible allowlist
  that never accepts a bypass-class mode. The model, effort and subagent model
  are offered, because they do not change what a session may do (D18).
- **The launcher is `scripts/jam`, not `bin/`.** The plugin documentation puts
  every `bin/` file on the Bash tool's PATH while the plugin is enabled, which
  would add an unrequested bare `jam` command that fails without `--data-dir` and
  contradicts D5's no-PATH rule, and claude.ai and Cowork do not install a plugin
  that has `bin/`. Nothing relies on the executable bit.
- **`python3 -I`.** Both entry points (and the hooks of D13) run the launcher as
  `python3 -I <plugin root>/scripts/jam`. Isolated mode ignores `PYTHON*`
  variables and the user site and keeps the script's directory off `sys.path`,
  so a stray `json.py` or a hostile `PYTHONPATH` cannot shadow the standard
  library; the launcher then puts `src/` first itself and writes no bytecode,
  because the versioned plugin cache is not jam's to write to. The interpreter is
  the one thing that must be on the PATH of the Claude Code process (3.9 or
  newer; the README states it). The install-only rule forbids settings edits, not
  a documented interpreter requirement.
- **Names.** The server key is `jam` and the tools are bare verbs, so the model
  sees `mcp__plugin_jam_jam__spawn`, `__list`, `__stop`, `__close`, and
  `__whoami`.
  Descriptive names would only be worth it if tool search were seen hiding them.
- **Verified later.** A print-mode session (`claude -p --plugin-dir .`, Claude
  Code 2.1.300, 2026-10-21) listed the server `plugin:jam:jam` as connected with
  the four tools, and a call to `list` returned the live sessions. On
  2026-10-28 (Claude Code 2.1.309) the server inside a `--bg` worker also
  answered `list`, which starts `claude` by its bare name, so in that run
  `claude` was on the PATH of an MCP server in a worker, on that machine. A
  worker's environment comes from the supervisor, not from the spawning shell,
  and how the supervisor was started was not recorded, so a machine whose
  supervisor starts with a narrower PATH may still fail. A failure reads "cannot
  run 'claude': executable not found"; add a binary override only if it happens.
- **Deferred.** Self-stop refusal in code (the short id is not reliably known
  over MCP, so the `stop` description warns instead), a `prune` verb (a `remove`
  one was added as `close`, D17), and the MCP extras (server instructions, pagination, structured output,
  cancellation). Server instructions are the extra that could reach a session
  that only reloaded the plugin, if Claude Code injects them at reload; that was
  not checked (#24). The SessionStart wiring and the notice text that were
  deferred here are D13, the foreign-repo guard (D6), deferred here as well, is
  D14, and the skill is D15.

- **Interrupts and a vanished reader.** An interrupt ends the command with exit
  130 (the shell's status for SIGINT) and nothing on stderr, because a traceback
  would only add paths. A reader that has gone away (a broken pipe) is never an
  operation failure: the stream's descriptor is pointed at `/dev/null`, the MCP
  server ends quietly, and a one-shot command exits 0. The write still raises, so
  the server loop stops. The same holds for `--help` and a usage error, which
  argparse prints into memory and the CLI then writes through the same guarded
  path: help still exits 0 and a usage error still exits 2, with no traceback and
  no text left in a buffer for the exit-time flush to fail on.
- **Argument checks.** A string that cannot be passed on, such as an unpaired
  surrogate (JSON can carry one; a command line cannot), is an invalid request,
  checked with `os.fsencode`. A usage error shows the failing command's own usage
  line, not the root one. The CLI spells the flags as `--include-exited` and the
  MCP schema as `include_exited`; both spellings come from the one table.
  Every key must be one the operation accepts, whatever it holds; only then does
  a null for an optional parameter count as absent, and a null for a required one
  is the wrong type.
- **Envelope checks.** A line is answered only when it is a JSON object whose
  `jsonrpc` is exactly "2.0"; anything else is -32600, with the request's `id`
  echoed when it is a valid one (a string or a finite number, never null, a
  boolean or a container) and null otherwise. Only then are client responses
  and notifications ignored. NaN, Infinity and -Infinity are parse errors
  (-32700), as strict JSON has none. A known method takes an object for
  `params` or none, and anything else is -32602; an unknown method is -32601
  whatever its `params`.

## D13. Hooks failure contract

Decided on 2026-10-20, from several independent designs reconciled into one. The
platform facts it relies on (hook timeouts, `async`, the SessionEnd budget, the
cap on injected context) are the ones recorded under "Plugin platform facts" by
the design pass; this change did not re-read the hooks reference. Three
SessionStart hooks ship here, all through the launcher as `python3 -I
"$CLAUDE_PLUGIN_ROOT/scripts/jam" hook <name>` (the exported variable, not the
placeholder; see D14, "Event and entry"). The launcher dispatches the first
argument `hook` before it imports the command line, so `notice` never loads the
parser, the registry or the service, and a test pins that import graph. The
registry, the sweep, the guard's modules and the spawn tickets are imported
lazily inside the handlers that use them, because `notice` needs none of them. A
fourth handler, `guard` (PreToolUse), is in the same table and is registered
through the same launcher, with `|| exit 0` appended; it is D14.

- **`notice`** has no matcher, so it fires again on resume, clear, compact and
  fork, which also re-injects the notice after a compaction. It prints the text
  that tells the model how to run jam: `python3 -I <root>/scripts/jam --data-dir
  <dir> COMMAND`, the commands, the MCP tool names, the session's own id,
  and the delegation rationale of D6 without mentioning the guard (D14-7).
- **`prune`** has the matcher `startup`, because it would otherwise run again on
  every resume, clear and compact. It calls `Registry.prune`, then sweeps the
  guard's state directory `guard-v1` (D14) and the spawn ticket directory
  `pending-v1` (D14). The guard's directory holds deny claims, hour slots, start
  records and worker marks; each file older than 30 days goes. Only the two
  guard sweeps, of the guard's state and of the spawn tickets, have a cap on the
  entries they look at, and they share a deadline of one second from the start
  of the hook. The note sweep of the registry (D11) has neither: it lists the
  whole notes directory, and that listing spends the same second, so the two
  guard sweeps may get no time and remove nothing in that start. Past two
  seconds the alarm ends the hook, and any step not yet run ends with it. A
  failure in one sweep does not stop the others. It prints nothing. The
  registry's own sweep stays by file age (D11).
- **`adopt`** has no matcher, so it fires again on resume, clear, compact and
  fork, as `notice` does. It writes the start record of the session, the real
  path of its start directory (D14), once. At a fresh start (any source other
  than `resume`, `clear`, `compact` and `fork`, so an unknown one counts) it
  also takes the freshest spawn ticket of that directory as the session's worker
  mark, by moving it there: a hard link, which never replaces a mark, then the
  removal of the ticket. It prints nothing and always exits 0. Its bounded work:
  one create, one capped listing, at most 8 moves; a lost mark costs at most one
  extra deny per repository. A ticket counts only when it was written at most
  300 seconds before the start (a ticket dated in the future counts as fresh),
  so a worker whose first session start comes later gets no mark.
- **No notice and no start record after a reload (observed once, #24).** On
  2.1.309 (2026-10-28), `/reload-plugins` in one running background session
  reported four more hooks, and no notice arrived and no start record was
  written there, which fits the notice and `adopt` not having run; that `prune`
  did not run is inferred, since it leaves nothing in an empty directory. What a
  `clear` or a `compact` does in a reloaded session was not tried. See
  "`/reload-plugins` in a running session" under "Plugin platform facts".
- **Synchronous, with explicit bounds, and no `async`.** A command hook's
  default timeout is 600 seconds, so each entry sets its own: 5 seconds for
  `notice` and `adopt`, and 3 for `prune`. Inside the process an alarm (3
  seconds for `notice` and `adopt`, 2 for `prune`) ends the hook by the default
  action of SIGALRM before the platform's timeout does; an ignored or a blocked
  SIGALRM is inherited across exec, so the hook resets the disposition and
  unblocks the signal first. `async` is not used: the reference lists it with no
  default stated, says Claude Code does not enforce a timeout on `async` hooks,
  and says nothing about plugin hooks, so an asynchronous prune could be shown
  neither to stay out of the way nor to be bounded. Rejected: `async: true` with
  a longer fallback timeout. A synchronous prune costs one directory scan of one
  small note per spawned session, once per `startup`, plus the sweep of the
  guard's state and of the spawn tickets (D14): two more scans of at most 28,800
  entries each, under a budget of one second, so those two stay bounded. The
  note scan has no cap of its own; only the alarm bounds it.
- **The notice is pure.** It does no file access at all, reads no environment
  variable but `CLAUDE_PLUGIN_DATA`, and uses nothing from the payload but the
  session id (accepted only if it passes the id check). SessionStart blocks the
  first response, so a stuck disk must not delay it and a bug in `prune` must not
  cost it. The plugin root is derived from the hook module's own location and
  pinned equal to the composition root's by a test; no root variable is read.
  Every path is quoted for a shell and `--data-dir` precedes the command word
  (D12). A data directory that is relative, longer than 1024 characters or holds a
  control character is left out, and the notice then says jam keeps no notes; an
  unusable plugin root makes it say the command line is unavailable and point to
  the MCP tools. The text is ASCII for ASCII paths, byte-identical for identical
  inputs (so a re-injection is harmless), well below the platform's cap on
  injected context, and carries no working directory, transcript path or
  exception text.
- **Fail open.** A hook never blocks: it never exits with 2. Every failure it
  can handle ends in exit 0 and leaves one stderr line that names only the
  exception type. An unusable payload (empty, not yet written to a non-blocking
  stdin, not UTF-8, not JSON, not an object, or over 256 KiB; the guard reads up
  to 8 MiB, D14) is not a failure:
  the notice is still printed, without the session id line. Only a crash, the
  alarm or a stdout that cannot be written leaves no output, and the alarm ends
  a hung hook by signal, so that exit status is not 0. A failed write to stdout,
  or to stderr when the crash report cannot be written, whatever its error,
  points the descriptor at `/dev/null` (the helper the
  command line shares, `silenced` in `src/jam/streams.py`), so the interpreter's
  flush at exit cannot fail again and turn exit 0 into 120. A closed stdout or
  stderr, which Python reports as `None`, is the same kind of failure and exits 0. In `prune` the registry sweep swallows `RegistryError`,
  `InvalidRequest` and `OSError`, the guard sweep and the ticket sweep never raise
  for a reason on disk, and an unexpected error in any of the three is reported by
  its type while the others still run. An unknown hook name does nothing, and only
  `adopt` reads `source`, which may take values not known yet.
- **The harness package holds all of it.** The notice names Claude Code tool
  names and CLAUDE.md, and the envelope is Claude Code's, so `notice.py`,
  `hook_io.py` and `claude_hooks.py` live under `src/jam/harness/`. Within
  `src/jam`, only the composition root imports that package from outside it,
  and a test pins that; the launcher `scripts/jam` enters
  `jam.harness.claude_hooks` directly for the `hook` verb, so a hook never loads
  the composition root and its wiring stays in the adapter.
  Rejected: the text and the envelope in the core, where a later harness would
  have to work around them.
- **SessionEnd: no hook.** A test pins the set of declared events, SessionStart
  and PreToolUse, so adding one is a visible change. The hooks reference gives
  SessionEnd a default timeout of exactly 1.5 seconds, shared by all of its
  hooks, that a plugin cannot raise (see "Plugin platform facts"), and the
  install-only rule forbids asking the owner to. It also fires on `clear` and
  `resume` while the conversation goes on, cannot block, and discards its
  output. Nothing is the right job for it. Removing the session's note would be
  wrong, since a stopped session stays in the full listing and a resumed one can
  return with the same id (D11). Removing guard state would re-arm a deny for a
  session about to continue (D6). Stale files (notes and guard claims) are swept
  by age at startup, which is the only cleanup. A hook that does nothing still
  costs an interpreter start on every exit. A later feature that truly needs
  exit-time work must use only lock-free single-file operations, stay far below
  1.5 seconds, and be a deliberate reviewed change.
- **Partly verified.** No session was started to test this change itself. A
  later print-mode session (`claude -p --plugin-dir .`, 2026-10-21) ran the
  notice at `startup` and returned its `additionalContext`. Observed later
  (2026-10-21, Claude Code 2.1.300, see D14): a `--bg` worker's first session
  start reported `source: startup` and the spawn directory as `cwd`, and its
  mark existed afterwards. The delay between the spawn and that first session
  start must also stay within 300 seconds for the mark to exist; the one run
  with a ticket measured 0.9 seconds. A `claude --bg --resume` started a copy
  whose session start reported `fork`, so `adopt` now skips the ticket for
  `resume` and `fork` as well, which only unit tests cover. An interactive
  session (`claude --plugin-dir .` in a trusted directory, not `--bg`, in a
  private tmux server with the `CLAUDE_*` variables of the parent session unset;
  2026-10-28, Claude Code 2.1.309) ran all three SessionStart hooks once each at
  `startup`, the prune among them, each finishing with status 0, and the notice
  arrived as a `hook_success` attachment carrying its `additionalContext`. The
  debug log, started with `--debug-file` with a second logging plugin loaded,
  reported 211, 212 and 213 ms for the three jam hooks; the other hooks, a tiny
  shell script among them, reported 210 to 221 ms, so about 0.21 seconds is an
  upper bound shared by hooks that start together, not a measure of any one of
  them. The model (Sonnet 5.5 at `xhigh` effort, in `auto` mode) was told to
  rely only on the notice (no MCP tool, no filesystem search) and to run
  `whoami` from the shell, and its first tool call was `whoami` through Bash in
  the form the notice gives. So the run shows that the model copied the form the
  notice gives, not that the notice made it choose the command. `id` was null,
  as D12 says it is in a foreground session: `CLAUDE_JOB_DIR` was unset there,
  as in a terminal outside any background session. The model's own explanation
  of the null was wrong (it said the session was not started by `spawn`), and
  the notice does not explain a null `id`; the `whoami` tool description does.
  It was one run, so it does not show that a model left to choose would use the
  notice unprompted.

## D14. The foreign-repository guard

Status: **Accepted** on 2026-10-20. The owner accepted D14-1, D14-2 and D14-3 as
proposed and authorised the live checks, and answered D14-7 and D14-8 on
2026-10-22; the other owner questions at the end hold their defaults until the
owner revises them. The guard is implemented, tested and **registered**:
`hooks/hooks.json` declares its PreToolUse entry (see "Event and entry"), and a
test pins that entry. The writers of the marks are in place (`adopt` and
`spawn`, see "Loop protection"), and the live checks it waited for ran on
2026-10-21 (see "Verified in real sessions"): a worker started after a spawn
ticket carried its mark from its first session start, which came well within the
300 seconds of D13, and was not denied. One point stays open: the skip of
`resume` and `fork` itself, which only unit tests cover; real sessions have
reported the sources `resume`, `fork` and `clear` (see "Resuming a background
session" and "Session variables" under "Plugin platform facts"). The skip can
only withhold a ticket, which costs a worker at most one extra denial per
repository. On a resume that keeps the id, the SessionStart hook saw
`CLAUDE_PROJECT_DIR` and `CLAUDE_CONFIG_DIR` with the values of the session's
start even when the caller's working directory, project directory or spelling of
the configuration directory differed (see "Resuming a background session"), so
the guard is expected to keep the start repository and configuration directory
there; no guarded edit was made in a resumed session. So registration did not
wait for that point, and it was registered once the owner approved the text the
model reads (D14-8). D6 states the intent; this entry is how it works.

- **Event and entry.** PreToolUse, matcher `Edit|Write|MultiEdit|NotebookEdit`
  (letters and `|` only, which the platform reads as an exact-name list; never
  `Edit.*`). MultiEdit is in no documentation page that was checked and stays
  because D6 and the issue name it; nothing may depend on it firing. The entry
  declared in `hooks/hooks.json` is `python3 -I
  "$CLAUDE_PLUGIN_ROOT/scripts/jam" hook guard || exit 0` with `"timeout": 5`.
  Tests pin that entry and that the in-process alarm (2 s) is below its timeout.
  The `|| exit 0` is the exit-2 wrapper: Python exits with 2 when it cannot open
  the script, which is what a stale plugin root after an update looks like, and
  2 blocks the tool call. The entry names the root through the exported
  variable, unbraced and in double quotes, so the shell expands it and never
  parses the characters of the root, and the quotes keep the expanded root one
  word, with no field splitting or pathname expansion. A `${CLAUDE_PLUGIN_ROOT}`
  placeholder would not be as safe: the plugins reference describes it as
  resolved inline in a hook command, and a root pasted into the command with an
  unmatched double quote or backtick, or an unterminated `$(`, in its name would
  make the shell exit with 2 before `||` runs and block every edit (#15), and a
  balanced `$(...)` in it would run as a command. Version 2.1.309 did not paste
  it (see "Hook command forms" under "Plugin platform facts"), but the unbraced
  variable is not a placeholder, so the entry is right either way. It needs only
  the export that the reference documents; without it the path would be
  `/scripts/jam`, which Python cannot open, and the wrapper would let every edit
  through. Tests run the entry from such a root and show that the braced form,
  pasted in, exits with 2. The exec form (`args`), which the hooks reference
  recommends for a command with a path placeholder, was weighed in #15 and not
  taken: it runs no shell, so keeping the wrapper would take `sh -c` with the
  root as a positional parameter, for no gain over the unbraced variable. No
  `onFailure` (it would turn failures into blocks), no `async`. The default
  command timeout is 600 s, so the explicit 5 is required. The alarm keeps the
  default SIGALRM disposition, so the kernel ends a hook stuck in a system call;
  the shell then reports 142 and the wrapper makes it 0.
- **Output.** Allow prints nothing and exits 0, so the normal permission flow
  stays in force: the guard never prints `allow`, `ask` or `defer`, and never
  exits 2. Deny is exactly one ASCII line and exit 0,
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"..."}}`,
  written only after the claims below exist. The reason (under 900 characters,
  `notice.deny_reason`) names the other repository's working-tree root when that
  is at most 200 characters and holds no control, format, line-separator or
  paragraph-separator character (Unicode categories Cc, Cf, Zl and Zp, and lone
  surrogates), and otherwise says "the repository containing the file". The root
  is a real path, so it can carry a directory name the model never typed (behind
  a link, or an alias such as `/tmp`), and the name of a clone is chosen by
  whoever published it; the length bound and the character filter are what keep
  that text harmless. The rest is a constant; its first sentence, "This is a
  one-time notice from jam, not a failure.", is the one D14-8 added, and the
  sentence that says to call `spawn` from the main conversation is the one D15-2
  added.
- **Decision order,** cheapest first; a step that allows returns before the next
  runs, so the costly ones (the git subprocess and the claims) happen only for
  an edit that would be denied, while the `.git` walks of step 7 are paid by
  every edit that reaches it. (1) The tool is one of the four (the adapter). (2)
  The session id is safe in a file name. (3) `worker.<id>` exists: allow. (4)
  The target path, relative paths joined to the call's working directory, is
  resolved with its non-existing tail kept, since a Write may create
  directories. (5) The start directory is `CLAUDE_PROJECT_DIR` when it is
  absolute, else the record `start.<id>`, else allow; the working directory of
  the call is never used, because it moves with every `cd`. (6) A target equal
  to or below the configuration directory (`CLAUDE_CONFIG_DIR`, else
  `~/.claude`) is allowed. The start directory is not an exclusion: a repository
  nested below it must still be told apart from the one the session started in.
  (7) A session that started outside any repository, a target outside any
  repository, and the same repository (a linked worktree counts as the same) all
  allow; any other repository, one nested below the start directory included
  (D14-6), is foreign. (8) `denied.<id>.<repo>` exists: allow. (9)
  `slot.<hour>.19` exists: allow. (10) One `git check-ignore -q` decides; only
  exit 1 continues, so an ignored path, an error and a timeout all allow. (11)
  Create the pair claim with `O_EXCL`; losing the race allows. (12) Create an
  hour slot; none free allows. (13) Print the denial. A crash or the alarm
  between 11 and 13 loses the nudge and can never produce a second one.
- **Repository identity is read from files, never from git.** From the nearest
  existing directory the walk looks at `.git` in at most 64 directories, the
  start included. A directory is the git directory. A regular file is read at
  most 4096 bytes and must start with `gitdir:`. The identity is the real path of
  the git directory named by its `commondir` file if there is one, else of the
  git directory itself, so a linked worktree equals its main checkout and a
  submodule (a git directory without `commondir`) is its own repository.
  Anything unreadable, oversized, malformed, a link or a pipe is "not a
  repository", which allows. The key in file names is the first 16 hex digits of
  the SHA-256 of the identity. Paths are compared as text after `realpath`, so on
  a case-insensitive file system two spellings of one directory compare unequal,
  which costs at most one extra denial.
- **The one subprocess** is `git -c core.fsmonitor=false check-ignore -q -- PATH`
  run in the nearest existing directory, with a timeout of 1 s, all three
  standard streams on `/dev/null`, and the environment stripped of every `GIT_*`
  variable plus `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0`, with `PATH`
  reduced to its absolute entries, or the platform's default search path when
  none is left: an empty or relative entry is looked up in the child's working
  directory, which is inside the foreign tree. Running there, git answers for
  the target's own repository, so an enclosing repository's ignore rules do not
  count: a nested clone that the superproject ignores is still judged by its
  own. The user's own git configuration still applies, so their global excludes
  count. A foreign repository's configuration can name an fsmonitor command,
  which is why that one is turned off. It runs only on the would-deny path and after the claim
  pre-checks, and the claim is not taken when git says "ignored" or cannot say.
- **State** lives under `<CLAUDE_PLUGIN_DATA>/guard-v1`, never in the target
  repository, the plugin root or `/tmp`. Directories are 0700 and created one
  level at a time, files 0600, whatever the umask. Everything is disposable and
  keyed by the full hook session id, so a resume that keeps the id keeps it,
  while `/clear` and a fork get a new id and the guard may speak once more per
  repository. There is no lock, no read-modify-write and no polling. `guard-v1`
  must be a real directory, not a link: with a link in its place every claim
  fails, every reader answers "nothing" and the sweep stands still, so the guard
  falls quiet instead of writing where the link points. The data directory above
  it belongs to the platform and is not checked. The check is one `lstat` before
  the use, and the gap between the two (time of check to time of use) is
  accepted, since only the same user can plant a link in the user's own plugin
  data directory.

  | File | Content | Written by | Read by |
  |---|---|---|---|
  | `start.<id>` | the session's start directory and a line feed | `adopt`, once | step 5, as the fallback |
  | `worker.<id>` | empty | `adopt`, by moving a spawn ticket | step 3 |
  | `pending-v1/<dir16>.<nonce16>` | empty, written by `spawn` before the harness runs | `spawn` | `adopt`, which moves the freshest one of its directory to `worker.<id>` |
  | `denied.<id>.<repo>` | empty | step 11 | steps 8 and 11 |
  | `slot.<hour>.<n>`, n below 20 | empty | step 12 | step 9 |

  `prune` removes deny claims and hour slots older than 30 days, with an entry
  cap and a deadline, and `prune` also removes start records and worker marks
  from `guard-v1` and spawn tickets from `pending-v1`, each file older than 30
  days. A worker mark's age is its file time, which the move keeps from the
  spawn ticket, so the mark lasts 30 days from the spawn, not from the worker's
  start. Ids admit dots, so every name pattern anchors the fixed-width suffix.
  The registry of D11 is untouched: it is keyed by the harness short id, which a
  hook cannot know. The ticket directory is keyed by `dir16`, the first 16 hex
  digits of the SHA-256 of the real path of the start directory, so a spawn and
  the worker's start agree on it when both spell the directory the same way
  after realpath; on a case-insensitive file system another spelling costs a
  lost mark, the same case limit as the repository identity above. `adopt` calls
  the writers in `jam.claims` and `jam.tickets` for the start record and the
  mark.
- **Loop protection** has three layers. A session with a `worker.<id>` mark is
  not denied while the mark exists. The mark is a bare file created by moving a
  ticket that `spawn` wrote before it started the worker, at the first fresh
  start in that directory (D13) within 300 seconds; it records that the session
  took the ticket, not that jam started it. Only a fresh start takes the ticket:
  `resume`, `clear`, `compact` and `fork` skip it (D13), and an unknown source
  takes it. A live `--bg` worker's first start reported `startup`, so the skip
  costs a worker nothing, and it keeps a resumed session or a copy from taking a
  ticket meant for a new worker. The mark is keyed by the session id, so a
  resume that keeps the id keeps the mark, while `/clear` and a fork get a new
  id and so start without one. On 2.1.309 a `claude --bg --resume` with no other
  flag, run 20 seconds or more after a stop, kept the id; one with `--plugin-dir`
  and a prompt added (the only flag variant tried), and one run within about 3
  seconds of a stop, started a copy with a new id. jam's marks were not part of
  those runs (see "Resuming a background session" under "Plugin platform
  facts"). The mark stays until the sweep removes it, 30 days after the spawn; after that the per-pair claim and
  the hourly limit still bound the loop. Independently of recognition, a denial
  is given at most once per (session, repository) through the `O_EXCL` claim,
  however often the edit is retried, the session resumed or the hook run in
  parallel, and at most 20 times per clock hour on the machine through the slot
  files. A deny never spawns anything, and a recognised worker is not denied
  while its mark exists, so a guard cannot push a worker to delegate again.
  Per-pair memory alone does not bound a chain, since every hop is a fresh
  session id; the hourly limit does, even when recognition fails at every hop.
  What is not bounded is a model that spawns in a loop on its own initiative,
  and ping-pong between peers: those belong to the skill (D15) and to
  `SendMessage`. The wrong assumptions cost different amounts. A session that
  takes a ticket it did not spawn (a fresh start in that directory within 300
  seconds of the spawn) is not denied in any repository while its mark exists,
  which is until the sweep: it misses one nudge per repository it edits in, and
  the permission flow is unchanged, so what is lost is the nudge, not a
  permission. A worker gets at most one extra denial per repository when another
  session took its ticket, when its first session start comes more than 300
  seconds after the spawn, or when it starts in a directory other than the spawn
  directory (D13). A missing data directory means the guard allows everything.
  No environment variable carries any of this, since the spawner's environment
  does not reach a worker (see "Plugin platform facts"); the guard reads only
  `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR`, `CLAUDE_CONFIG_DIR` and, for the
  default configuration directory, `HOME`.
- **Fail open.** Allow (print nothing, exit 0) on: stdin over 8 MiB, not JSON,
  not an object or wrong-typed fields; a path with NUL, or relative without an
  absolute working directory; an invalid session id; a data directory that is
  unset, relative or not creatable; a state directory that is a link; any I/O
  error in the `.git` walk; a walk over 64 levels; git missing, slow or
  answering anything but 1; any failed claim; any exception (reported by type
  only); a broken stdout. The alarm, a missing interpreter and a missing script
  all end in exit 0 through the wrapper, whatever characters the plugin root
  holds (see "Event and entry"). A hook timeout does not: it kills the shell
  before the wrapper can run, and the platform treats it as a non-blocking
  failure, so the call goes ahead. The guard never waits on a lock, never
  sleeps, never reads stdin without a cap, never runs code a foreign repository
  controls except the one git call above (which finds `git` only through the
  absolute entries of `PATH`, since an empty or relative entry would resolve
  inside the foreign tree), never writes into the target repository or the
  plugin root, and never puts exception text or edit content on stdout.
- **Import cost.** The hot path loads only the standard modules the hook already
  needs, plus the guard's own small modules. `argparse`, `subprocess`,
  `tempfile`, `hashlib` and the registry are imported only on the rare paths,
  and a test pins that the allow path loads none of them.
- **Core and adapter.** The decision (`jam.guard`), the repository identity
  (`jam.repos`), the claims (`jam.claims`), the spawn tickets (`jam.tickets`),
  the sweep and the file checks (`jam.files`) are stdlib-only and name no
  harness; a test pins their imports and that no Claude Code word appears in
  them. Which tool calls are edits, where the path sits in the call, the
  variable names and the deny envelope (`hook_io.py`) are in the harness
  package, which wires the guard itself, as D13 describes for the other hooks.
- **Verified in real sessions (2026-10-21, Claude Code 2.1.300).** A throwaway
  copy of the plugin, with the guard registered and a hook that logged every
  input, was loaded with `--plugin-dir` into `--bg` workers that ran under
  `bypassPermissions`, and each worker was followed by `stop`, `rm` and a
  re-list. PreToolUse fires inside a `--bg` worker. There `CLAUDE_PROJECT_DIR`
  is the spawn directory and `CLAUDE_CONFIG_DIR` the configuration directory,
  and both stay the same after `EnterWorktree`. The one resume tried then,
  `claude --bg --resume` from the same directory with `--plugin-dir`, `--model`,
  `--permission-mode`, `--disallowedTools` and a prompt added, started a copy
  with a new id, which had the same values; a resume that keeps the id was seen
  later, and its SessionStart hook saw the values of the session's start (see
  "Resuming a background session" under "Plugin platform facts"). A JSON denial
  holds under `bypassPermissions`. In both runs that were denied, the original session and the copy its resume started, a
  Haiku worker whose prompt asked it to decide what to do after a refusal
  re-issued the denied edit, as the reason invites it to: it saw the reason as
  `PreToolUse:Write hook error: <reason>`, wrote the same file again, and the
  second attempt passed because of the claim file. Whether other models and
  prompts do the same is not known. The copy that a resume started has a new id,
  so it was denied once more in the same repository.
- **The worker's first session start (verified the same day).** After a ticket
  was written the way `spawn` writes it, a `--bg` worker started in that
  directory reported `source: startup` and the spawn directory as `cwd` 0.9
  seconds later. `guard-v1/worker.<id>` existed afterwards with the ticket gone
  and its file time kept, and the worker's edit in another repository passed
  with no denial. The probe ran `claude --bg` itself instead of going through
  `spawn`; `tests/test_spawn_wiring.py` covers `spawn` and `adopt` together. The
  copy of the plugin ran the `adopt` of the main branch at the time, which
  skipped only `clear` and `compact`, and no ticket was waiting when the resumed
  copy started; the skip of `resume` and `fork`, added to the `RESUMED` tuple of
  `src/jam/harness/claude_hooks.py` after the probe, is covered by unit tests
  only.
- **The entry under an awkward plugin root (verified 2026-10-24, Claude Code
  2.1.309).** A copy of the plugin in a directory whose name holds a double
  quote, a backtick and `$(` was loaded with `--plugin-dir` into print-mode
  Haiku sessions. With the braced form the entries had then, the hooks ran as
  under any other root. The notice named the launcher under that directory,
  quoted, and the guard denied the first `Write` to a repository outside the
  configuration directory once and let the re-issued call through. The unbraced
  form declared now did the same. In that run the MCP server connected too. A
  first try wrote into a repository under the job directory, which is inside the
  configuration directory, so D14-3 let it through without a denial; a probe of
  the guard needs a target outside it. So the failure #15 describes did not
  occur on that version: the platform did not paste the root into the command
  (see "Hook command forms" under "Plugin platform facts").
- **Rejected.** An environment counter, which does not reach workers. A registry
  note keyed by the short id: it would be written after the spawn returns, which
  is the race of D6, and a hook cannot derive the short id. A marker in the spawn
  prompt, which D6 once preferred: neither SessionStart nor PreToolUse input carries
  the prompt, so recognition would depend on parsing the transcript, whose shape
  and flush timing at the first PreToolUse are unverified and whose failure mode
  is a silently dead guard; it also rewrites every worker brief and can be
  forged. It stays the documented fallback if the ticket handshake cannot work.
  Exempting every background session, which would disable the guard for
  background orchestrators. A machine-wide limit of 6 per 600 s, too tight for
  normal work with several sessions. Taking the working directory of the call as
  the start directory.
- **Peers (D10).** The mark is a flag that records the ticket was taken, fixed
  at creation. It is not a depth, a role or a parent link, no spawner id is
  stored, and a peer message cannot change it, so sessions stay flat.
- **Owner questions.** Answered on 2026-10-20: D14-1, D14-2, D14-3 and item 9
  accepted as proposed. Answered on 2026-10-22: D14-7 and D14-8, each recorded
  under its number below. The others hold their defaults until the owner revises
  them. Each default below is one isolated constant or one clause of
  `jam.guard`, marked with its number, and a test names it. D14-2 is the
  exception: its two clauses (steps 9 and 12) go together, with the throttle's
  members of `Effects` and `Claims`.
  1. D14-1 (accepted), worker exemption: a session that took a spawn ticket
     written by `spawn` is not denied, recognised by the mark that the ticket
     became (step 3). The exemption lasts while the mark exists; the 30-day
     sweep ends it (see the state paragraph). This revises D6's preference for a
     marker in the prompt. Alternative: a hop budget carried in the ticket,
     which keeps the nudge inside delegated chains at the cost of more state.
  2. D14-2 (accepted), throttle: 20 denials per clock hour on the machine (steps 9 and 12,
     over `SLOTS_PER_HOUR`). It is a named departure from "exactly once per
     session and repository" when it binds, sized never to bind in real use.
     Dropping it takes both clauses, because step 9 checks the hour before git
     is asked and step 12 claims the slot after the pair, and with them the
     members `hour`, `throttled` and `claim_slot` of `Effects` and their
     counterparts in `Claims`. Alternatives: tune it, or drop it and rely on
     recognition alone.
  3. D14-3 (accepted), configuration directory: edits under it are never denied (step 6),
     a fifth exclusion beyond D6's four, because auto-memory, plan and rule writes
     land there and the directory can itself be a repository on dotfile setups.
  4. D14-4, subagents: the guard applies inside them. Nothing reads `agent_id`,
     since it also appears for whole `--agent` sessions; the first agent to edit
     gets the one denial, and what a denied subagent does next is in D15.
     Alternative: skip events that carry it.
  5. D14-5, a session that started outside any repository is never denied
     (step 7), because "the repository the session started in" is undefined.
     Alternative: treat every repository as foreign.
  6. D14-6, nested repositories: a submodule, and any repository nested below
     the start directory (a nested clone, a vendored checkout with its own
     `.git`), is a separate repository, because identity is its own git
     directory. It gets the one nudge wherever in the superproject the session
     started, unless that repository's own ignore rules, or the user's global
     excludes, cover the path. Alternative: part of the superproject, which
     would bring back a shortcut for the start directory and make the outcome
     depend on where the session started.
  7. D14-7, an off switch or a repository allowlist: install-only forbids
     requiring a settings edit, not offering an escape. And whether the notice
     should mention the guard (default: no). Answered on 2026-10-22: no off
     switch and no allowlist for now, and the notice does not mention the guard.
  8. D14-8, approval of the fixed product text the model reads: the deny reason
     and the notice. Answered on 2026-10-22: the notice is approved as written,
     and the deny reason with one sentence added at its start, "This is a
     one-time notice from jam, not a failure.", because the model sees a denial
     labelled as a hook error (see "Verified in real sessions"); the rest of the
     reason was unchanged until D15-2 added one sentence on 2026-10-23.
  9. Authorisation (granted) for the live checks listed under "Verified in real
     sessions", and for the check of `claude --bg --session-id` in "Plugin
     platform facts". The `--session-id` check was answered on 2026-10-21: the
     flag is ignored under `--bg`. The live checks ran the same day. Each real
     spawn is followed by `stop`, `rm` and a re-list.
  10. Shape of the work: the core merged without registering anything. The
      writers of the spawn tickets and the `worker.<id>` marks are in place
      (D13, `adopt`), and the live checks above ran, leaving open one point
      whose cost is bounded (see the status above), so the change that
      registered the hook waited only for D14-8, which the owner answered on
      2026-10-22.

## D15. The delegation skill

Decided on 2026-10-22. One skill ships, `skills/delegate/SKILL.md`
(`/jam:delegate`), and the model loads it when its description fits the task. It
teaches what the tool descriptions and the notice state in a line or not at all:
when to delegate, how to brief a worker so that it can report back, how to wait
for it and answer it with the native messaging, and the bounds that D14 leaves
to the skill (a model that spawns in a loop on its own initiative, and ping-pong
between peers).

- **What it relies on.** `SendMessage` and `ListAgents`: a session is addressed
  by the name `ListAgents` prints, the first line of `ListAgents` names the
  calling session as others address it, a reply copies the `from` attribute of
  the incoming `<cross-session-message>`, and `notify_when_idle: true` asks for
  one notice when a session on this machine next goes idle. `notify_when_idle`
  works only in the main conversation, and a subagent's sends go out under its
  parent session's address, so the skill spawns and waits only from the main
  conversation. These come from the tool descriptions of Claude Code 2.1.300 and
  from one listing seen on 2026-10-22, not from a documentation page, so a later
  version may change them. A call with `notify_when_idle: true` and an empty
  `message` was refused as an invalid parameter on 2.1.309 (2026-10-28), while
  the same call with a one-line message subscribed, so the skill asks for one
  line and says that the session receives it.
- **Subagents.** The guard runs in a subagent too, and the one denial per
  repository is shared with the main conversation (D14-4). The deny reason and
  the `spawn` description therefore say to call `spawn` from the main
  conversation and to hand the work back from a subagent, and the skill adds
  that the subagent says jam suggested delegating it. The main conversation then
  decides, and is not denied a second time for that repository.
- **What it leaves out.** It repeats a rule of the tool descriptions only where
  its guidance builds on it, for example the complete brief, peer messages as
  input, the session name, replies, and never stopping its own session, closing
  one only when the owner asks (D17), so a
  change to one of those descriptions may call for the same change here. It
  leaves out what they cover alone, such as the `problem` of a spawn result, a
  timeout, an untrusted workspace, the characters of a `name`, `include_exited`,
  an `@` attachment and the first-line preview of a message. It names no path:
  jam is run through its MCP tools or through the command line of the notice
  (D5).
- **Text.** Like the notice (D14-8), the skill is product text that the model
  reads. Its `description` falls under the default 1,536-character cap of the
  skill listing (see "Plugin platform facts"), and a test keeps the description
  under 400 characters, since the listing carries it on every turn, and the body
  under 100 lines.
- **Verified.** A print-mode session (`claude -p --plugin-dir .`, Claude Code
  2.1.300, 2026-10-22) listed the skill as `jam:delegate` among its skills and
  slash commands. No session has used it yet, so whether the model loads it when
  a task fits its description is not observed.
- **Rejected.** One skill per role, spawner and worker: the same session can be
  both (D10), and each skill adds a listing entry to every turn. The guidance
  in the notice instead: the notice is injected at every session start and
  after every compaction (though not by `/reload-plugins` in the one session
  checked; see "Plugin platform facts"), while a skill body loads only when it
  is used.
- **Owner questions.** Answered on 2026-10-23: D15-1, the owner asked for a
  shorter text without the redundant parts, so the skill now leaves to the tool
  descriptions what they say alone; D15-2, yes: the deny reason now says to call
  `spawn` from the main conversation, and a subagent to hand the work back.

## D16. The Devin CLI harness

Decided on 2026-10-29. D4 scoped v1 to Claude Code; the owner then asked for
Devin CLI support and named Codex as a future candidate, so the adapter seam is
now exercised by a second harness. The core is untouched: everything Devin
lives in `harness/devin.py` and `harness/devin_env.py`, and `compose` picks by
environment.

What Devin is, verified on devin 3000.11.92 (2026-10-29): there is no
daemon-managed background stack, no `--bg` and no `SendMessage`. A session is a
row in `~/.local/share/devin/cli/sessions.db` (SQLite, `sessions` table) that
`devin -p "<prompt>"` creates and then runs; the row persists and the session
is resumable after the process exits. `devin -r <id> -p "<message>"` resumes
it and answers synchronously; `devin list --format json` lists the sessions of
the directory it runs in; `devin rm <id>` deletes the record. An untrusted
directory makes `devin -p` refuse with "untrusted workspace" on stderr.

The adapter's mapping:

- **spawn** → `devin -p` detached (`Popen`, new session, stdin devnull, output
  to a temp log read only on early exit). The new id is discovered by diffing
  `devin list` in the target directory before and after the launch, under a
  bounded poll (20 s). The spawned process's pid is written to a pidfile next
  to the store so `stop` can end a run still in flight. `name` is refused:
  Devin picks the id itself, and a wrong claim of support is worse than a
  refusal.
- **list** → `devin list --format json` in the given `cwd` (the CLI is
  directory-scoped). With no `cwd` the adapter reads `sessions.db` read-only
  through stdlib `sqlite3`, excluding `hidden` rows unless `include_exited`.
  The schema is internal, so the CLI stays the preferred path wherever a
  directory applies. `state` is reported as `resumable`: rows are at rest, and
  claiming a run-state the store does not expose would invent it.
- **stop** → SIGTERM the recorded pid when it still is a devin process (a `ps`
  probe guards against a reused pid), then `devin rm`. There is no
  stopped-but-listed state: the row is the session, so nothing is returned.
- **remove** → `devin rm`, then a store re-check.
- **whoami** → an error, not a guess. Devin exports no session id to a
  session's own environment (only a traceparent and an internal db pointer);
  ids reach hooks through the payload but not the Bash a `whoami` runs
  through. A wrong id is worse than none (D12); the error names `list` +
  `cwd` matching as the fallback.
- **Messaging** is not a jam verb on either harness: the delegate skill names
  `devin -r <id> -p "<message>"` for Devin. Send and result are fused — the
  call returns the peer's reply directly, with no idle wake.

Hooks and packaging need no second implementation. Devin's binary carries
Claude's hook schema wholesale (`ClaudeHookConfig`,
`ClaudeHookSpecificOutput` with the full event set), honors the
`hookSpecificOutput` envelope including `permissionDecision`, and expands
`${CLAUDE_PLUGIN_ROOT}` — observed through a dual-harness plugin whose
`SessionStart`/`PreToolUse`/`Stop` entries run unchanged on both. The only
manifest change is the PreToolUse matcher gaining Devin's lowercase tool
names. Hook-side environment resolution picks per detected harness: Devin's
variables are `PLUGIN_DATA`, `DEVIN_PLUGIN_ROOT`, `DEVIN_PROJECT_DIR`,
`AI_AGENT=devin_*`; the deny path also reads `~/.devin` as its config dir.
Devin's hook payload carries `session_id` and `cwd` like Claude's, so `adopt`
and the guard work unchanged.

Harness selection: `detect` in `devin_env` marks a Devin context
(`AI_AGENT=devin_*`, `DEVIN_PROJECT_DIR`, or `PLUGIN_DATA`); anything else is
Claude. The composition root is still the only module that names an adapter,
and env modules are still the only ones that name variables.

Rejected:

- **`devin acp` as the transport**: ACP sessions die with their stdio process
  and cannot be resumed across calls the way the persisted CLI sessions can.
- **The database as the only `list` path**: an internal schema behind an
  official listing command is the fallback, not the default.
- **A `send`/`message` verb**: keeps the verb surface identical across
  harnesses; the skill teaches the native primitive.
- **Codex and further harnesses**: still out of scope; what this decision
  proves is the seam.

## D17. Closing a session

Decided on 2026-10-29, from issue #32 and the owner's request. When the owner
explicitly asks for a session to be closed, `stop` is the wrong verb: on Claude
Code a stopped session stays in `claude agents --all` until it is removed (see
"Plugin platform facts"), and jam had no way to remove it, because D12 deferred
a `remove` verb.

- **The verb.** `close` takes one short `id`, and is one more row of the
  operations table (D12), so the CLI (`jam close ID`) and the MCP tool
  (`mcp__plugin_jam_jam__close`) print the same line. It stops the session, then
  deletes it through the adapter's `remove`, which re-checks the full listing.
  The result is `{"id", "closed": true}`, and anything else is a failure.
  Stopping comes first for every session, with no check of whether it is
  running: the core cannot tell on every harness (a Devin row never carries a
  `status`), a stop ends a run that is still going, and a second `claude stop`
  of a session that is stopped but still listed exits 0 (see "Verified"). A
  session that is no longer listed once it is stopped is not deleted a second
  time: that is the Devin CLI, where stopping removes the row (D16). An id the harness does not know fails at the stop, in the harness's words. The Claude adapter passes on only the whole short id (eight hexadecimal characters, the form of the spawn output), because `claude stop` and `claude rm` match the start of an id: a prefix would stop one session, find no row with that exact id, and read as a session that stopping had deleted.
- **Who may use it.** Only when the owner asked to close or remove the session.
  `stop` stays the verb for a session a model started and has nothing left to do for, and on Claude Code it keeps the conversation. On the Devin CLI stopping already deletes the record (D16), so there the two end the same way and only the owner's say-so separates them; the `stop` description says so. That answers the question issue #32
  left to the owner, whether a model may delete the sessions it started: not on
  its own initiative. Nothing in code can tell who is asking (sessions are
  peers, D10), so, like the self-stop warning (D12), the tool description and
  the skill (D15) carry the rule.
- **Refusals are never forced.** `claude rm` has two flags that override a
  refusal, `--discard-unpushed` for unpushed commits and uncommitted changes and
  `--force-remove-worktree` for a worktree that cannot be removed, and each takes
  a value that a previous `claude rm` reported. jam passes neither. A failure of the removal comes back as the harness's own words behind "the session was stopped, but removing it failed: ". The note leads so that clipping a long message cannot drop it, and it claims only that the removal step failed, because a failed re-check after a successful `rm` reads the same; `list` says whether the session is still there. `claude rm` reports a refusal on stdout, not stderr, together with the commands that override it (read from the strings of the 2.1.309 binary; no live refusal was seen), so the adapter relays the reason lines and drops any line that names `--discard-unpushed`, `--force-remove-worktree`, `ctrl+x` or the `claude rm` command. The owner decides what to do with the worktree and asks again.
- **The registry.** A closed session's note stays until the sweep removes it by
  age (D11). The listing only annotates rows that exist, so the note is never
  shown, and `close` leaves the registry alone, as `stop` does.
- **Rejected.** A flag on `stop` that also deletes: one verb that sometimes
  destroys a conversation behind a boolean, where a model told to stop is one
  wrong argument from deleting. A bulk verb that removes every stopped session
  (D12's `prune`): a deletion should name one id. Documenting the owner's own
  `claude rm` only: it leaves the model without an answer when the owner says
  "close it". Skipping the stop for a session that is not running, which the
  first draft did by reading a missing `status` as stopped: on the Devin CLI no
  row has a `status`, so a run still in flight would have lost its record and
  its pidfile and gone on running.
- **Verified (2026-10-29, Claude Code 2.1.309).** On throwaway workers, `close`
  of a busy one and of one that `stop` had left listed (`state: stopped`, no
  `status`) each ended with the session gone from `list --include-exited`. A
  second `close` of a closed id failed with "stopping <id> failed (exit 1): No
  job matching '<id>'", and an id with a space failed as an invalid id before
  `claude` ran. A second `claude stop` of a stopped, still listed session
  printed `stopped <id>` and exited 0. The adapter's command sequence and the
  refusal text are covered by unit tests against a scripted `claude`. A real refusal, over unpushed commits or uncommitted changes, was not provoked here, so the stdout form of its text is unobserved,
  and `claude rm` on a running session without a stop was not tried: its help
  says it also "works on already-exited sessions", so the stop stays first. The Devin side is covered by unit tests only, through the real adapter over a scripted `devin` and a temporary store. Observed once and not explained: two
  workers that had finished a turn (`state: done`, `status: idle`, a live pid)
  were gone from `claude agents --all` within about half a minute, before any
  `stop` or `rm` was run on them, so a session can vanish between the listing
  and the deletion.

## D18. Choosing the model per session

Decided on 2026-11-02, from issue #35 and the owner's request after a day of
parallel background sessions in one repository. Every worker ran on whatever
model and effort the owner's settings held at the moment it started, so design
work, hard debugging, documentation and mechanical edits all got the same tier.

- **The parameters.** `spawn` takes three optional choices, rows of the one
  operations table (D12): `model` (`--model`), `effort` (`--effort`) and
  `subagent_model` (`--subagent-model`). An omitted one leaves the owner's
  setting in charge. The Claude adapter maps them to `claude --bg --model M
  --effort E --settings '{"env":{"CLAUDE_CODE_SUBAGENT_MODEL":"S"}}'`.
- **Why the model may choose them, unlike `permission_mode` (D12).** A
  permission mode decides which tool calls run without the owner's approval, so
  a mode chosen by the model would go around D8. A model, an effort and a
  subagent model change what a session costs and how well it reasons, never
  what it may do: every permission rule, hook and first-use approval applies as
  before. A poor choice gives a slower, costlier or weaker worker, which the
  spawner can stop. The tier table the owner uses is personal tuning and lives
  in the owner's own configuration; the skill (D15) gives only the generic rule.
- **The subagent model travels with the session.** The owner's subagent model
  is one global setting that concurrent sessions share, and it can be rewritten
  while workers run, so a worker would otherwise inherit whatever another
  session last wrote. `--settings` with a JSON string gives that one process
  the variable and writes no file. An environment variable set by the spawner
  would not do: it does not reach the worker (see "Plugin platform facts").
- **Checks.** A model is an alias or an id of letters, digits, `.`, `_` and
  `-`, at most 64 characters, that does not start with `-`, so it never reads
  as a flag; effort is one of `low`, `medium`, `high`, `xhigh` and `max`. Like a
  name, they are the adapter's to check (D12), with the pattern and the levels
  kept in the core so both adapters use the same ones. jam keeps no list of
  model names, since that list changes with versions and accounts. On Claude
  Code an unknown name is not refused: the session starts and falls back to the
  default model (see "Verified").
- **The Devin CLI (D16).** `model` maps to `devin --model`. Devin has no
  per-session effort and no subagent model, so either is refused before
  anything runs, in the same way as a name.
- **Verified (2026-11-02, Claude Code 2.1.295).** A throwaway worker started
  through the command line of this change with `--model sonnet --effort low
  --subagent-model opus`, while the owner's settings named `haiku` as the
  subagent model, recorded `claude-sonnet-5-5` with effort `low` on every turn
  of its transcript, its subagent recorded `claude-opus-5-5`, and `echo
  $CLAUDE_CODE_SUBAGENT_MODEL` in its Bash printed `opus`. The checksum of the
  user settings file was the same before and after. A worker started with
  `--model no-such-model-x1` was not refused: its screen showed "Switched to
  Opus 5.5 because no-such-model-x1 is not available", and it answered on the
  default model. The Devin side is covered by unit tests only.
- **Rejected.** Exposing `permission_mode` alongside (D12 stands). A list of
  allowed models in jam: it would go stale, and which model fits which task is
  the owner's tuning. Setting `CLAUDE_CODE_SUBAGENT_MODEL` in the spawner's
  environment: it does not reach the worker. `--fallback-model`: nothing asked
  for it.

## D19. The project in a session's name

Decided on 2026-11-02, from issue #36. With jam used in several projects at
once, the `claude agents` screen did not show which session belonged to which
project.

- **The label.** The project is named by a directory: the main checkout of the
  repository the spawn directory belongs to, else the spawn directory itself. A
  linked worktree therefore names its main checkout, not the worktree's own
  directory, which is usually named after a branch. The repository is found by
  reading files, as the guard does (D14), so no `git` process runs. The
  directory name is cut down to the characters a name allows: every run of other
  characters becomes `-`, and leading and trailing `.`, `_` and `-` go. When
  nothing is left, there is no label.
- **The name.** A name the caller gives becomes `<label>-<name>`, unless it
  already is the label or starts with the label and a dash, compared without
  case. The caller's part is never cut: the label is shortened to keep the whole
  within 64 characters, and left off when none of it fits. An invalid name is
  passed on unchanged, so the adapter refuses it as before.
- **No name, no prefix.** When the caller gives no name, jam passes none. In a
  check on 2026-11-02 a session started without one was titled from its brief by
  Claude Code (`jam-test-throwaway-check`), while one started with `-n jam` kept
  `jam` after its turn. Naming every unnamed session with the label alone would
  replace those titles with one name shared by all of them, and `SendMessage`
  refuses a name that two sessions share until the `[ref]` is added. The
  `spawn` description and the skill ask for a name.
- **Which harness.** An adapter declares whether its sessions take a name. The
  Claude adapter does; the Devin adapter refuses a name (D16), so nothing is
  prefixed there. The rule lives in the core (`naming`), and the service applies
  it before the adapter runs.
- **Verified (2026-11-02, Claude Code 2.1.295).** A worker spawned with
  `--name check-model` in this repository's main checkout came back as
  `jam-check-model` from `spawn`, was listed under that name by `claude agents
  --json`, and a `SendMessage` to the bare name `jam-check-model` reached it and
  drew its reply.
- **Rejected.** `git rev-parse --show-toplevel`: it starts a process and names
  a worktree after its own directory. The label alone as the name of an unnamed
  session, for the reason above. Cutting the caller's name to make room: two
  names that differ only at the end would become one.

## Plugin platform facts

Unless marked as verified, these facts were taken from the official Claude Code
documentation through a research pass and have not been re-verified one by one;
re-check the source before relying on a single claim.

Sources: the plugin
[manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference)
(layout, environment variables, plugin settings, data directory), the
[hooks guide](https://code.claude.com/docs/en/hooks-guide) (hook behavior), and
[installing plugins](https://code.claude.com/docs/en/plugins/install)
(marketplaces, private repositories), the
[MCP page](https://code.claude.com/docs/en/mcp) (plugin MCP servers, tool
names), the
[environment variables page](https://code.claude.com/docs/en/env-vars)
(session variables), and the MCP specification, revision 2025-11-25
([transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle),
[tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)).
The MCP and environment items below were re-read against those pages on
2026-10-20. Items labeled "client behavior" are not in the public documentation:
they were seen in the shipped client during the design of D12 and are not
re-verified by a session. The install version requirements were
checked against the release notes for
[2.1.275](https://github.com/anthropics/claude-code/releases/tag/v2.1.275) and
[2.1.292](https://github.com/anthropics/claude-code/releases/tag/v2.1.292).

- **Layout.** `.claude-plugin/plugin.json`, `hooks/hooks.json` (the file needs a
  top-level `"hooks"` key), `.mcp.json` (jam declares its server in the
  manifest instead, see "Plugin MCP servers" below), `skills/`, `bin/`. A plugin
  `bin/` is added to the Bash tool's PATH while the plugin is enabled, and
  claude.ai and Cowork do not install a plugin that has one; jam does not rely on
  it (see D5) and keeps its launcher in `scripts/` instead (D12).
- **Skills.** A plugin skill lives at `skills/<name>/SKILL.md` and loads as
  `/<plugin>:<name>`, where `<name>` is the frontmatter `name` when it sets one
  and the directory name otherwise. The skill listing always holds every skill's
  name. Each skill's `description` and `when_to_use` are capped together at
  1,536 characters by default (`skillListingMaxDescChars` changes the cap), and
  when the listing outgrows its budget, 1% of the context window by default, the
  skills invoked least lose theirs first. The body loads only when the skill is
  used, and the documentation advises keeping it under 500 lines. A frontmatter
  field the platform does not know is ignored without an error. Source: the
  skills page of the Claude Code documentation, read on 2026-10-22.
- **Path variables.** `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` are exported
  to hook processes and MCP stdio servers, but not to commands the model runs
  through the Bash tool. This is why a SessionStart hook has to inject the
  resolved path (D5). `CLAUDE_PLUGIN_DATA` survives plugin updates, so the
  registry lives there instead of a temp directory. Uninstalling the plugin from
  its last scope deletes that directory unless `--keep-data` is passed. jam
  treats this state as disposable: the registry holds only jam's own notes about
  sessions (today, which ones jam spawned), while the harness stays the source of
  truth for which sessions exist. Losing a note costs only its annotation, though
  a lost `spawned` note cannot be rebuilt from the harness. Nothing that must
  outlive an uninstall is stored there.
- **Hooks.** Plugin hooks behave like settings hooks. `additionalContext` must
  be nested in `hookSpecificOutput`; at the top level it is silently ignored,
  and what a hook injects is capped at 10,000 characters. A command hook has a
  default timeout of 600 seconds, so a plugin hook sets its own. The reference
  lists `async` as an optional command-hook field with no default stated, says
  Claude Code does not enforce a timeout on `async` hooks, and says nothing
  about plugin hooks; jam sets it nowhere (D13). A Stop hook can block with
  `decision: block` but only a limited number of consecutive times (check
  `stop_hook_active`). SessionEnd hooks have a default timeout of exactly 1.5
  seconds, shared by all of them; timeouts set on plugin-provided hooks do not
  raise it (only a hook timeout in the user's own settings or the user's
  `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` does). SessionEnd also fires with
  the reasons `clear` and `resume` while the conversation goes on, so it does
  not mean the session is over. It cannot block termination and its JSON output
  is discarded. Not documented: whether it fires when a background worker is
  stopped with `claude stop`. Stale notes and guard files are therefore swept by age at startup
  (`prune`), never at the end of a session (D13).
- **Hook command forms (observed on 2.1.309, 2026-10-24).** In the shell form,
  without `args`, the platform passes the command to `/bin/sh -c` exactly as
  written, with `${CLAUDE_PLUGIN_ROOT}` and `$CLAUDE_PLUGIN_ROOT` left as text,
  and the shell expands them from the exported variables: a probe hook printed
  its own command line with `ps`, and a single-quoted `'${CLAUDE_PLUGIN_ROOT}'`
  came out unexpanded. In the exec form (`args`, added in 2.1.139),
  `${CLAUDE_PLUGIN_ROOT}` in an argument is replaced by the path as plain text,
  and the unbraced `$CLAUDE_PLUGIN_ROOT` is not. The plugins reference describes
  the shell form otherwise: its table of where each variable resolves lists
  "Anywhere in `command` and `args`" for hook commands, and it says that on
  Windows the substituted paths use forward slashes so that a shell does not
  read backslashes as escapes, which is a value pasted into the command. It also
  lists `CLAUDE_PLUGIN_ROOT` as exported to hook processes. The release notes
  fit what was observed: 2.1.246 fixed hook error messages that showed a literal
  `${CLAUDE_PLUGIN_ROOT}` instead of the path, and 2.1.281 added a validator
  warning for an unquoted one in a shell-form hook, which breaks on a path with
  spaces. Only macOS was tried, and older versions were not. jam's hook entries
  use the unbraced variable, which is right under either reading (D14).
- **Permissions.** A plugin cannot ship `permissions.allow`; only `agent` and
  `subagentStatusLine` are honored from a plugin's `settings`. The documented
  workaround is a `PermissionRequest` hook that allows the plugin's own MCP
  tools. That is a plugin approving itself, so its matcher must be as narrow as
  possible, or the first-run user approval is kept instead.
- **Install.** The repository carries `.claude-plugin/marketplace.json`. Users
  run `/plugin marketplace add GeneralD/jam`, then `/plugin install` with the
  plugin and marketplace name; this two-step form needs no particular version.
  One-step forms exist too: the in-session `/plugin install <plugin>
  --marketplace <source>` (verified in the 2.1.275 release notes) and the shell
  `claude plugin install <plugin> --marketplace <source>` (the `--marketplace`
  flag was added to the shell command in 2.1.292; the command itself is older).
  Auto-update is off by default for third-party marketplaces. There is no
  install-time hook, so initialization happens lazily on first use.
- **`/reload-plugins` in a running session (observed once on 2.1.309,
  2026-10-28).** jam was installed at user scope with `/plugin install` while a
  background session was already running, and `/reload-plugins` was run in that
  session. The reload reported one more plugin, four more hooks and one more
  plugin MCP server, and the jam tools became available there. No SessionStart
  notice arrived in that session, and the plugin's data directory stayed empty
  afterwards. Two things were observed: no notice, and an empty data directory.
  `adopt` writes the start record into that directory (D14), so the empty
  directory fits `adopt` not having run. `prune` would leave nothing in an empty
  directory, so that it did not run is inferred from the missing notice and the
  missing record. So in that session the reload reported the hooks and made the
  MCP tools available, and the SessionStart hooks appear not to have run,
  although a session start runs them (D13). For comparison, a print-mode
  `claude -p --resume <id>` of a session that had been created with jam disabled
  loaded the plugin, connected the MCP server and ran the SessionStart hooks
  with `source: resume`, notice included. What the gap costs in that session:
  the tool descriptions carry the instructions for using the tools; no worker
  mark is involved (a mark is taken at a fresh start, D13); the guard reads
  `CLAUDE_PROJECT_DIR` first and the start record only when that is not
  absolute, so a missing record matters only then, and the guard then allows
  (D14, step 5), and whether the guard ran in the reloaded session was not
  checked; the notice is what gives the model the exact command line of the CLI,
  with the data directory flag, which matters only when the MCP server is not
  available. Not checked: other Claude Code versions; a foreground session; the
  hooks of a second running session, which was seen to start a jam MCP server
  after its own reload; whether a `clear` or a `compact` in a reloaded session
  runs the hooks; the Devin CLI; and whether a reload re-injects the MCP server
  `instructions`, which D12 defers and which would reach a reloaded session if
  it did (#24).
- **Background sessions (verified by experiment).** The documentation is
  silent, so a throwaway probe plugin was loaded with `--plugin-dir` into both a
  print-mode run (control) and a `claude --bg` session. In both, the plugin's
  stdio MCP server started, and the SessionStart, PostToolUse, and Stop hooks
  fired with `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` exported. So a
  background worker runs jam's hooks and MCP server like any other session.
  Not tested: a plugin installed from a marketplace (the load path is expected
  to be the same).
- **Background sessions need a trusted workspace.** `claude --bg` refuses to
  start in a directory whose workspace trust was never accepted, printing
  "Workspace not trusted. Run `claude` in <dir> once and accept the trust
  prompt, then retry." `spawn` must detect this, stop, and tell the owner which
  directory to trust, instead of retrying or silently failing. Observed: the
  command exits with status 1, writes the message to stderr, names the resolved
  path, and starts no session. A fresh temporary directory outside the owner's
  config was refused, while a new subdirectory under a directory that holds a
  trusted project was accepted, so trust may be inherited from an ancestor. In
  the one case tried (2026-10-21), a git repository created inside a trusted
  project was refused, so any such inheritance did not reach that nested
  repository, and whether trust is inherited at all is still unverified. jam
  therefore does not pre-check trust: it runs the command and reacts to the
  error.
- **Environment variables do not reach `--bg` workers (verified by
  experiment).** A variable set on the shell that runs `claude --bg` was absent
  in the worker: asked to run `printenv` on it through its Bash tool, the
  worker answered that it was unset. Workers are started by the supervisor
  from a pre-warmed pool, not from the spawning shell. Anything a worker must
  know has to travel in the prompt or in a file.
- **Background session commands (observed on 2.1.300 and 2.1.309).** `claude
  --bg [-n name] [--permission-mode mode] -- <prompt>` prints `backgrounded ·
  <id> · <name>` (with terminal colors) and exits 0; the `--` makes a prompt
  that starts with a dash safe. The rows of `claude agents --json` for
  background sessions have `id`, `sessionId`, `name`, `cwd`, `kind`,
  `startedAt`, `state`, and, while the session runs, `pid` and `status` (`busy`
  or `idle`). `claude stop` removes a session from that plain listing at once;
  with `--all` the row stays, without `pid` or `status`, until `claude rm`,
  after which it is gone from both. The `state` seen right after a stop was
  `stopped` in two runs and `done` in a third; on 2.1.309 (2026-10-28) it was
  `done` for two sessions that had finished a turn and `stopped` for two copies
  that never started one. It is treated as a pass-through string. The help text
  says the plain listing also includes
  interactive sessions, and it did on 2.1.309 (2026-10-28): a foreground session
  is a row with `kind: interactive` and `pid`, `cwd`, `startedAt`, `sessionId`,
  `name` and `status`, but no `id` and no `state`. A session's transcript is a
  JSONL file in the project directory named after its `sessionId`; `claude
  logs` shows only a terminal redraw. A row once came back without `id`; it may
  have been such a row, but the cause was not established. The adapter skips a
  row without a usable id, so `list` and `stop` do not reach a foreground
  session, and the native `ListAgents` shows it by name.
- **Cross-session messages and permission modes (observed on 2.1.309, 2026-10-24
  and 2026-10-28).** The `SendMessage` tool description says that a session
  running in a different permission mode holds cross-session messages for its
  user's approval and may let them expire, and that a `[Cross-session delivery
  notice]` tells the sender when a session on the same machine holds or refuses
  one. In the first check (2026-10-24), a background session in `auto` exchanged
  messages in both directions with `--bg` workers started in `auto`, `manual`,
  `acceptEdits`, `plan`, `dontAsk` and `bypassPermissions`. The worker started
  by jam's `spawn`, which passes no mode, ran in `auto`, the
  `permissions.defaultMode` of that configuration. The worker started with `manual` ran in what its
  transcript records as `default`. No message was held and no `[Cross-session
  delivery notice]` arrived. Delivered messages carried `from-mode="prompting"`
  for five of those modes and `from-mode="bypass"` for `bypassPermissions`, so
  the harness sorts modes into at least two classes, and a message across them
  was not held either. A foreground session was tried once, on 2026-10-28: an
  idle interactive session in `auto`, started in a tmux session of its own and
  also connected through Remote Control, was sent a message by name from a
  `--bg` worker started without a permission-mode flag (a worker started that
  way ran in `auto` on 2026-10-24; its mode and the `from-mode` of the delivered
  message were not recorded in this run). `SendMessage` reported success, with
  the message in the session's inbox and not yet read, and the foreground
  session then woke on its own, read it and replied. No message was held. It
  was one run in a tmux pane; a plain terminal and a zellij pane were not tried,
  nor was a foreground session in another mode, and what makes a session hold a
  message is not known.
- **Stop, then remove (last checked on 2.1.309, 2026-10-24).** Once, `claude
  stop` followed by `claude rm` printed success while the session stayed in
  `claude agents` as idle, and a second stop and rm removed it. It did not recur
  in five later runs, nor on 2026-10-24, when seven workers were each stopped
  and removed in one round and a re-list matched the listing taken before they
  started. The adapter does not retry: `remove` checks the full listing and
  reports a session that is still there.
- **`--session-id` under `--bg` (verified by experiment, 2026-10-21, Claude Code
  2.1.300).** Passing `--session-id <uuid>` to `claude --bg` does not fix the
  session's id: the command prints `warning: --bg manages the session id;
  ignoring --session-id`, and the session gets the id the platform assigns. The
  short id in the `backgrounded` line was the first 8 characters of `sessionId`
  in that observation, and the same held for every background session seen
  since, among them the eight of 2026-10-24. No document states it, so jam never
  derives the short id from `sessionId`: it takes it from the spawn output, and
  `whoami` uses the relation only to reject a job directory that belongs to
  another session (D12).
- **The background isolation guard (observed once on 2.1.300, 2026-10-21).** In
  one `--bg` worker (Haiku, under `bypassPermissions`), the first edit inside
  its own checkout was refused with a tool error, not a hook error: "This
  background session hasn't isolated its changes yet. Call EnterWorktree first
  so edits land in a worktree instead of the shared checkout, then retry this
  edit using the worktree path". The probe's hook logged every PreToolUse call
  and has none for that attempt, so the refusal most likely comes before the
  plugin hooks run; that order is inferred, not observed. The refusal names
  `"worktree": {"bgIsolation": "none"}` in that repository's
  `.claude/settings.json` as the way to turn it off; that setting was not tried.
  The other workers, which started outside any repository and edited another
  one, were not refused by it. That worker then edited in
  `.claude/worktrees/<name>`, a linked worktree of the same repository, which
  jam's guard counts as the start repository (D14), so it added no denial there.
  While that worktree held uncommitted changes, `claude rm` refused to remove
  the session.
- **Resuming a background session (observed on 2.1.300 and 2.1.309).** On
  2.1.300, `claude --bg --resume <id>` with `--plugin-dir`, `--model`,
  `--permission-mode`, `--disallowedTools` and a prompt added, run right after
  `claude stop <id>` had printed `stopped <id>` and with no listing read in
  between, printed `note: session <id> is already running in the background, so
  this started a copy as <new id>`. The copy's session start reported `source:
  fork` with a new session id, and had the same `cwd`, `CLAUDE_PROJECT_DIR` and
  `CLAUDE_CONFIG_DIR` as the original. The form without those flags was not
  tried on 2.1.300. On 2.1.309 (2026-10-28), a resume with no other flag, run
  within about 3 seconds of a stop, started a copy with the same note, twice,
  once after a plain listing had been read in between. Neither copy ran a turn
  or left a transcript; the session-start log showed nothing for the first copy
  and was not read for the second, so their `source` was not seen. After a
  pause of 20 seconds or more (about 20, 20, 44 and 65 seconds were tried), the
  same command with no other flag woke the same session: the note said it was
  woken with its saved options (`--plugin-dir`, `-n` and `--model`), the id was
  kept, and the session start reported `source: resume`, although the woken
  session ran no turn and was listed as idle. No resume with no other flag was
  tried between 3 and 20 seconds after a stop. With `--plugin-dir` and a prompt
  added, the only flag variant tried on 2.1.309, run about 8 seconds after a
  stop, the note said that the background session keeps its own saved options,
  so the flags passed started a copy under a new id, and the copy reported
  `source: fork`; the note named the flags, not the timing, and no other flag
  was tried. The SessionStart hook of a resumed session saw the `cwd`,
  `CLAUDE_PROJECT_DIR` and `CLAUDE_CONFIG_DIR` of the session's start, not the
  caller's, in two runs: in one the caller ran from a subdirectory; in the other
  it set `CLAUDE_CONFIG_DIR` with a trailing slash (the same directory, spelled
  another way) and another `CLAUDE_PROJECT_DIR` together. Neither variable was
  changed alone, and a different configuration directory was not tried.
- **The data directory of a `--plugin-dir` plugin (observed once on 2.1.300).**
  A plugin named `jam-probe`, loaded with `--plugin-dir`, had
  `CLAUDE_PLUGIN_DATA` set to `<config>/plugins/data/jam-probe-inline`, where
  `<config>` is the configuration directory. A session reports such a plugin as
  `<name>@inline`, and the documented data directory replaces every character of
  the plugin's identifier outside `A-Z a-z 0-9 _ -` with `-`, so `<name>-inline`
  is expected for any name.
- **Install from the public repository (verified by experiment).** With the
  manifests on `main`, `claude plugin marketplace add GeneralD/jam` registered a
  marketplace named `jam` and `claude plugin install jam@jam` installed the
  plugin at version 0.1.0 in user scope, enabled. `claude plugin uninstall
  jam@jam` followed by `claude plugin marketplace remove jam` restored the
  plugin and marketplace lists to exactly what they were before. Two details:
  because the plugin source is the repository root (`./`), the installed copy is
  the whole repository (docs, tests, and assets included, under 1 MB for now),
  and uninstalling leaves the cached copy on disk. If the payload grows, move it
  to a subdirectory and point the marketplace entry at it. On 2026-10-24 (Claude
  Code 2.1.309), the add and install commands were run against an empty
  configuration directory (`CLAUDE_CONFIG_DIR`) and were the whole setup: a
  print-mode session there ran jam's three SessionStart hooks with exit 0,
  received the notice, and listed `plugin:jam:jam` as connected with its four
  tools. The model call then failed because that directory had no login, which
  is Claude Code's own setup, not jam's. A recursive comparison with `main`
  (outside `.git`, `.claude` and `__pycache__`) found only the harness's
  `.in_use` marker added.
- **Plugin MCP servers (documented).** A plugin declares them in `.mcp.json` at
  its root as `{"mcpServers": {"<server>": {...}}}`, or inline in `plugin.json`
  under `mcpServers` (a map keyed by server name, a path to another file, or an
  array of both), which loads after `.mcp.json` and replaces a server of the same
  name. jam uses the inline map: a `.mcp.json` at the repository root is also read
  as the project-scoped MCP configuration of any session started in a clone, and
  `${CLAUDE_PLUGIN_ROOT}` is not set there. A session that starts the server from
  that scope (`claude -p`, which loads project servers without asking, or an
  interactive session once the server is approved) runs the unexpanded text and
  fails to connect, and an interactive session that has not approved it shows it
  as pending approval. Observed on 2026-10-21: `claude mcp get jam` named the
  scope "Project config (shared via .mcp.json)" and reported a failed connection.
  In a stdio server's
  `command`, `args`, and `env`, `${CLAUDE_PLUGIN_ROOT}` and
  `${CLAUDE_PLUGIN_DATA}` are substituted, and both are exported to the server
  process. Tool names are `mcp__plugin_<plugin>_<server>__<tool>`, with every
  character outside `A-Z a-z 0-9 _ -` replaced by `_`; jam's are
  `mcp__plugin_jam_jam__<verb>`. For a `.mcp.json` file the documentation shows
  both the wrapped shape and one without the wrapper, with the server names at the
  top level.
- **stdio framing (MCP specification, 2025-11-25).** Messages are delimited by
  newlines and must not contain embedded newlines; there is no `Content-Length`
  header. stdout carries protocol messages only, and stderr may carry logs. Client
  behavior: Claude Code splits at LF and strips a trailing CR.
- **Protocol version (client behavior).** Claude Code requests `2025-11-25` and
  accepts a reply of `2025-11-25`, `2025-06-18`, `2025-03-26`, or `2024-11-05`
  and rejects any other. The lifecycle rule is that a server echoes
  a requested version it supports and otherwise answers one it supports. jam
  echoes a requested version from that list, else answers `2025-11-25`, and never
  answers a version outside it (for example `2026-07-28`).
- **`server/discover` probe (client behavior).** Before `initialize`, Claude Code
  sends a `server/discover` request to a disposable second server process, and an
  unanswered request stalls the connection for up to 60 seconds. jam answers any
  unknown request that has an id with `-32601` at once. Because of the probe, the
  server may start twice per session start, so startup, `initialize`,
  `tools/list`, and unknown methods must do no I/O and create nothing.
- **Session variables (documented; observed on 2.1.309).**
  `CLAUDE_CODE_SESSION_ID` is set in Bash and PowerShell tool subprocesses, hook
  command subprocesses, and stdio MCP server subprocesses. For Bash and hooks it
  matches the hook input's
  `session_id` and follows `/clear`; an MCP server keeps the id it was started
  with (the resumed id for `--resume <session-id>`, and possibly the initial
  startup id for `--continue` or `--resume` without an id). `CLAUDE_JOB_DIR` is
  set in each background session to that session's jobs directory, and shell
  commands the session runs inherit it; it is documented for nothing else, so it
  is absent in a foreground session unless that session was started from the
  shell of a background session, which exports it to everything it starts (#20),
  and not guaranteed to an MCP server. Observed in a background session on
  2.1.309 (2026-10-28): after `/clear`, `CLAUDE_CODE_SESSION_ID` in Bash held a
  new id and the job directory kept its name, so `jam whoami` through Bash
  reported the new `session_id` with a null `id`; the session start reported
  `source: clear` with the new id, and the session's row in `claude agents
  --json` kept its `id` and held the new `sessionId` in the first listing read
  after it, about 4 seconds later (one session, attached in a terminal).

## Acceptance check (2026-10-24, Claude Code 2.1.309)

The three acceptance criteria of the design issue were checked once, end to end,
from a background session in `auto` mode.

- **Installing is the only setup.** See "Install from the public repository"
  under "Plugin platform facts": the marketplace add and install, run against an
  empty configuration directory, were enough for the three SessionStart hooks
  and the MCP connection, and only the model call needed a login.
- **Spawn, prompt, result.** A print-mode session that loaded the installed copy
  with `--plugin-dir` called `spawn` once through the MCP server, with a prompt
  that asked the worker to compute a product and send it to the background
  session by name. The worker did, and the background session that ran the check
  received the result as a cross-session message. Through jam's CLI the
  background session also spawned a worker itself, and a follow-up message
  reached that worker and was answered.
- **The guard denies once per session and repository.** In a print-mode session
  started in one git repository with the same copy loaded, the first `Write` to
  a second git repository outside the configuration directory was denied with
  the one-time notice, and the same `Write`, re-issued, created the file.
- **Found.** In that print-mode session, `whoami` returned its own `session_id`
  with the short id of the background session whose shell had started it,
  because the session inherited `CLAUDE_JOB_DIR` (#20). Fixed afterwards:
  `whoami` reports a null `id` in that case (D12).

Every worker was then stopped and removed, and a re-list matched the listing
from before.

## Open verification items

None from the list of 2026-10-24: its four items were checked on 2026-10-28
(Claude Code 2.1.309), and the results are in D2, D11, D12, D13, D14 and "Plugin
platform facts". Points that are still unverified are named where they occur,
among them the skip of `resume` and `fork` by `adopt` (D14), the notice being
used by a model that is not told to rely on it (D13), a foreground session in a
permission mode other than `auto`, in a plain terminal or in a zellij pane
("Cross-session messages and permission modes" under "Plugin platform facts"),
a resume under a different configuration directory ("Resuming a background
session" under "Plugin platform facts"), and what `/reload-plugins` gives a
running session beyond the one background session on 2.1.309 that was checked:
other versions, a foreground session, a second session's hooks, and whether a
reload re-injects the MCP server `instructions` ("`/reload-plugins` in a running
session" under "Plugin platform facts").
