"""Smoke tests for scripts/jam, run as `python -I scripts/jam ...` in a subprocess.

A fake `claude` shell script is the only `claude` on PATH, so no real session is
ever touched. Only POSIX is supported (the registry needs fcntl).
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "jam"
SESSION_ID = "aaaa1111-0000-4000-8000-000000000001"

# Like the real one (Claude Code 2.1.295), it refuses to run in a working directory that was deleted.
FAKE_CLAUDE = """#!/bin/sh
/bin/pwd -P >/dev/null 2>&1 || { echo "error: The current working directory was deleted, so that command didn't work." >&2; exit 1; }
case "$1" in
  agents)
    printf '%s\\n' '[{"id":"aaaa1111","sessionId":"aaaa1111-0000-4000-8000-000000000001","name":"worker-a","cwd":"/work/repo-a","kind":"background","state":"working","status":"busy","pid":4242,"startedAt":1700000000000}]'
    ;;
  *)
    echo "unexpected claude invocation: $*" >&2
    exit 97
    ;;
esac
"""

PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode("utf-8") + b"\n"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


@unittest.skipUnless(os.name == "posix", "the launcher smoke tests need a POSIX shell")
class LauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(0o755)
        self.path = os.pathsep.join([str(self.bin), "/usr/bin", "/bin"])
        self.assertEqual(shutil.which("claude", path=self.path), str(fake), "the fake claude must shadow any real one")

    # The variables of every supported harness are stripped: CLAUDE* (some hold
    # credentials) and the Devin set, so no ambient harness leaks into the subprocess.
    OTHER_HARNESS_VARS = ("AI_AGENT", "PLUGIN_DATA", "PLUGIN_ROOT", "CHISEL_SESSION_DB")

    def env(self, **extra):
        """The parent environment minus every harness variable (some hold credentials), on a closed PATH."""
        kept = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CLAUDE", "DEVIN_")) and key not in self.OTHER_HARNESS_VARS
        }
        return {**kept, "PATH": self.path, **extra}

    def jam(self, *args, stdin=b"", cwd=None, env=None, launcher=LAUNCHER, flags=("-I",)):
        proc = subprocess.run(
            [sys.executable, *flags, str(launcher), *args],
            input=stdin,
            capture_output=True,
            cwd=str(cwd or self.root),
            env=self.env() if env is None else env,
            timeout=60,
        )
        proc.out, proc.err = proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8", errors="replace")
        return proc

    def assertExit(self, proc, code):
        self.assertEqual(proc.returncode, code, f"stderr: {proc.err}")

    def jam_without_reader(self, *args, stdin=b""):
        """Run jam with a stdout whose reader is already gone, so its first write fails with a broken pipe."""
        reader, writer = os.pipe()
        os.close(reader)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(LAUNCHER), *args],
                input=stdin,
                stdout=writer,
                stderr=subprocess.PIPE,
                cwd=str(self.root),
                env=self.env(),
                timeout=60,
            )
        finally:
            os.close(writer)
        proc.err = proc.stderr.decode("utf-8", errors="replace")
        return proc

    def server(self, cwd=None):
        """A running `jam mcp` with all three pipes open. It is killed at cleanup if a test leaves it running."""
        proc = subprocess.Popen(
            [sys.executable, "-I", str(LAUNCHER), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd or self.root),
            env=self.env(),
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),  # a backgrounded parent may have ignored it
        )
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()

    def reply_from(self, proc, request=PING):
        """Send one request and read its reply line while the server's stdin stays open."""
        proc.stdin.write(request)
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 15)
        self.assertTrue(ready, "no reply while stdin was open: the server must flush after every reply")
        return json.loads(proc.stdout.readline())


class RunTest(LauncherTestCase):
    def test_help_exits_zero_and_lists_the_commands(self):
        proc = self.jam("--help")
        self.assertExit(proc, 0)
        for command in ("spawn", "list", "stop", "close", "whoami"):
            self.assertIn(command, proc.out)

    def test_mcp_answers_exactly_two_lines_and_writes_nothing_to_stderr(self):
        data = "".join(json.dumps(message) + "\n" for message in (INITIALIZE, INITIALIZED, TOOLS_LIST)).encode("utf-8")
        proc = self.jam("mcp", stdin=data)
        self.assertExit(proc, 0)
        self.assertEqual(proc.err, "")
        replies = [json.loads(line) for line in proc.out.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual([tool["name"] for tool in replies[1]["result"]["tools"]], ["spawn", "list", "stop", "close", "whoami"])

    def test_decoy_modules_and_a_hostile_pythonpath_are_ignored(self):
        decoy, hostile = self.root / "decoy", self.root / "hostile"
        for folder in (decoy, hostile):
            folder.mkdir()
            (folder / "json.py").write_text("raise ImportError('decoy json was imported')\n", encoding="utf-8")
            (folder / "argparse.py").write_text("raise ImportError('decoy argparse was imported')\n", encoding="utf-8")
        proc = self.jam("--help", cwd=decoy, env=self.env(PYTHONPATH=str(hostile)))
        self.assertExit(proc, 0)
        self.assertNotIn("decoy", proc.out + proc.err)
        self.assertIn("usage:", proc.out)

    def test_it_writes_no_bytecode_into_the_plugin_tree(self):
        tree = self.root / "plugin"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        for folder in ("scripts", "src", ".claude-plugin"):
            shutil.copytree(ROOT / folder, tree / folder, ignore=ignore)
        for args in (("--help",), ("list",)):
            self.assertExit(self.jam(*args, launcher=tree / "scripts" / "jam"), 0)
        self.assertEqual([str(path) for path in tree.rglob("__pycache__")], [])
        self.assertEqual([str(path) for path in tree.rglob("*.pyc")], [])


class WiringTest(LauncherTestCase):
    def test_the_plugin_source_comes_ahead_of_anything_already_importable(self):
        decoy = self.root / "decoy"
        (decoy / "jam").mkdir(parents=True)
        (decoy / "jam" / "__init__.py").write_text("", encoding="utf-8")
        (decoy / "jam" / "cli.py").write_text("raise ImportError('decoy jam was imported')\n", encoding="utf-8")
        # Without -I: under it nothing could come ahead of the source, and the order would not be seen.
        proc = self.jam("--help", env=self.env(PYTHONPATH=str(decoy)), flags=())
        self.assertExit(proc, 0)
        self.assertNotIn("decoy", proc.out + proc.err)

    def test_it_finds_the_source_when_the_launcher_file_is_a_symlink(self):
        folder = self.root / "links"
        folder.mkdir()
        (folder / "jam").symlink_to(LAUNCHER)
        proc = self.jam("--help", launcher=folder / "jam")
        self.assertExit(proc, 0)
        self.assertIn("usage:", proc.out)


class ServerTest(LauncherTestCase):
    def test_a_reply_arrives_while_stdin_is_still_open(self):
        proc = self.server()
        self.assertEqual(self.reply_from(proc)["id"], 1)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertEqual(proc.stderr.read(), b"")

    def test_it_still_lists_after_the_directory_it_started_in_is_deleted(self):
        started_in = self.root / "worktree"
        started_in.mkdir()
        proc = self.server(cwd=started_in)
        self.assertEqual(self.reply_from(proc)["id"], 1)  # running before the directory goes
        started_in.rmdir()
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list", "arguments": {}}}
        result = self.reply_from(proc, json.dumps(call).encode("ascii") + b"\n")["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        self.assertEqual([row["id"] for row in json.loads(result["content"][0]["text"])["sessions"]], ["aaaa1111"])

    def test_invalid_utf8_is_a_parse_error_and_the_server_goes_on(self):
        invalid = b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xff"}\n'
        japanese = json.dumps({"jsonrpc": "2.0", "id": "日本語", "method": "ping"}, ensure_ascii=False).encode("utf-8") + b"\n"
        proc = self.jam("mcp", stdin=invalid + japanese)
        self.assertExit(proc, 0)
        self.assertEqual(proc.err, "")
        first, second = [json.loads(line) for line in proc.out.splitlines()]
        self.assertEqual((first["id"], first["error"]["code"]), (None, -32700))
        self.assertEqual((second["id"], second["result"]), ("日本語", {}))

    def test_an_interrupt_ends_it_quietly_with_the_conventional_code(self):
        proc = self.server()
        self.assertEqual(self.reply_from(proc)["id"], 1)  # the loop is running, so the signal cannot land during startup
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=15), 130)
        self.assertEqual(proc.stderr.read(), b"")

    def test_a_lone_surrogate_in_a_prompt_is_a_bad_argument_not_a_started_session(self):
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "spawn", "arguments": {"cwd": str(self.root), "prompt": "hi \ud800"}}}
        proc = self.jam("mcp", stdin=json.dumps(call).encode("ascii") + b"\n")  # json.dumps escapes the surrogate
        self.assertExit(proc, 0)
        result = json.loads(proc.out)["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("internal error", result["content"][0]["text"])
        self.assertNotIn("may have started", result["content"][0]["text"])


class VanishedReaderTest(LauncherTestCase):
    def test_the_server_ends_quietly_when_nobody_reads_its_output(self):
        proc = self.jam_without_reader("mcp", stdin=PING * 3)
        self.assertExit(proc, 0)
        self.assertEqual(proc.err, "")

    def test_a_command_does_not_fail_when_nobody_reads_its_output(self):
        proc = self.jam_without_reader("list")
        self.assertExit(proc, 0)
        self.assertEqual(proc.err, "")


class ListTest(LauncherTestCase):
    def test_without_a_data_directory_the_rows_come_back_with_a_problem(self):
        proc = self.jam("list")
        self.assertExit(proc, 0)
        self.assertNotIn("Traceback", proc.out + proc.err)
        found = json.loads(proc.out)
        self.assertEqual([row["id"] for row in found["sessions"]], ["aaaa1111"])
        self.assertIsNotNone(found["problem"])

    def test_with_a_data_directory_there_is_no_problem_and_nothing_is_created(self):
        data = self.root / "data"
        proc = self.jam("--data-dir", str(data), "list")
        self.assertExit(proc, 0)
        found = json.loads(proc.out)
        self.assertIsNone(found["problem"])
        self.assertEqual(found["sessions"][0]["spawned"], None)
        self.assertFalse(data.exists())

    def test_the_environment_variable_stands_in_for_the_flag(self):
        proc = self.jam("list", env=self.env(CLAUDE_PLUGIN_DATA=str(self.root / "data")))
        self.assertExit(proc, 0)
        self.assertIsNone(json.loads(proc.out)["problem"])


class WhoamiTest(LauncherTestCase):
    def test_it_prints_the_session_id_and_the_job_name(self):
        env = self.env(CLAUDE_CODE_SESSION_ID=SESSION_ID, CLAUDE_JOB_DIR="/jobs/aaaa1111")
        proc = self.jam("whoami", env=env)
        self.assertExit(proc, 0)
        self.assertEqual(json.loads(proc.out), {"session_id": SESSION_ID, "id": "aaaa1111"})

    def test_it_fails_with_one_line_when_the_session_id_is_unset(self):
        proc = self.jam("whoami")
        self.assertExit(proc, 1)
        self.assertEqual(proc.out, "")
        self.assertTrue(proc.err.startswith("jam: "), proc.err)
        self.assertEqual(proc.err.count("\n"), 1)
        self.assertNotIn("Traceback", proc.err)


if __name__ == "__main__":
    unittest.main()
