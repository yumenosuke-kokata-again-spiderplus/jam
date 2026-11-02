"""Tests for the stdio MCP server: newline-delimited JSON-RPC over the operations table."""

from __future__ import annotations

import contextlib
import importlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts src/ on sys.path)
from jam.errors import HarnessError
from jam.harness.claude import RunResult
from jam.session import Session

from test_claude_harness import LIVE, FakeRunner, agents
from test_operations import SESSION_ID, FailingStopHarness, elsewhere, identity, lazy, operations, raising, runtime_for
from test_service import FakeHarness, session

mcp = lazy("jam.mcp")
compose = lazy("jam.compose")

VERSION = "9.8.7"
SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
NEWEST = SUPPORTED[0]


def request(method, params=None, id=1):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_call(name, arguments=None, id=1):
    params = {"name": name} if arguments is None else {"name": name, "arguments": arguments}
    return request("tools/call", params, id)


def encoded(message):
    return message if isinstance(message, (bytes, str)) else json.dumps(message).encode("utf-8")


def answer(message, runtime=None):
    reply = mcp.handle_line(runtime_for() if runtime is None else runtime, encoded(message), VERSION)
    return None if reply is None else json.loads(reply)


def tool_result(reply):
    """(isError, text) of a tools/call reply that carries exactly one text content."""
    (content,) = reply["result"]["content"]
    if content["type"] != "text":
        raise AssertionError(f"not a text content: {content!r}")
    return reply["result"]["isError"], content["text"]


def serve_lines(runtime, lines):
    written = []
    mcp.serve(runtime, iter(lines), written.append, VERSION)
    return written


class InitializeTest(unittest.TestCase):
    def test_each_supported_version_is_echoed(self):
        for version in SUPPORTED:
            with self.subTest(version=version):
                reply = answer(request("initialize", {"protocolVersion": version, "capabilities": {}}))
                self.assertEqual(reply["result"]["protocolVersion"], version)

    def test_anything_else_gets_the_newest_supported_version_and_never_2026(self):
        for params in ({"protocolVersion": "2026-07-28"}, {"protocolVersion": "1999-01-01"}, {}, {"protocolVersion": 5}, {"protocolVersion": None}, {"protocolVersion": [NEWEST]}, {"protocolVersion": ""}):
            with self.subTest(params=params):
                reply = answer(request("initialize", params))
                self.assertEqual(reply["result"]["protocolVersion"], NEWEST)
                self.assertNotIn("2026-07-28", json.dumps(reply))

    def test_the_result_names_the_server_and_advertises_tools_only(self):
        reply = answer(request("initialize", {"protocolVersion": NEWEST}, id=7))
        self.assertEqual((reply["jsonrpc"], reply["id"]), ("2.0", 7))
        self.assertEqual(reply["result"]["capabilities"], {"tools": {}})
        self.assertEqual(reply["result"]["serverInfo"], {"name": "jam", "version": VERSION})
        self.assertNotIn("instructions", reply["result"])


class MethodTest(unittest.TestCase):
    def test_ping_answers_an_empty_result(self):
        self.assertEqual(answer(request("ping"))["result"], {})

    def test_notifications_are_never_answered(self):
        for method in ("notifications/initialized", "notifications/cancelled", "notifications/never-heard-of", "tools/list", "ping", "server/discover"):
            with self.subTest(method=method):
                self.assertIsNone(answer({"jsonrpc": "2.0", "method": method}))
                self.assertIsNone(answer({"jsonrpc": "2.0", "method": method, "params": {}}))

    def test_unknown_methods_with_an_id_fail_at_once(self):
        for method in ("server/discover", "resources/list", "prompts/list", "completion/complete", "nonsense"):
            with self.subTest(method=method):
                reply = answer(request(method))
                self.assertEqual((reply["error"]["code"], reply["error"]["message"]), (-32601, "Method not found"))
                self.assertEqual(reply["id"], 1)
                self.assertNotIn("result", reply)

    def test_there_is_no_lifecycle_state(self):
        runtime = runtime_for()
        self.assertEqual([tool["name"] for tool in answer(request("tools/list"), runtime)["result"]["tools"]], ["spawn", "list", "stop", "close", "whoami"])
        self.assertFalse(tool_result(answer(tool_call("whoami"), runtime))[0])
        self.assertEqual(answer(request("server/discover"), runtime)["error"]["code"], -32601)
        for _ in range(2):
            self.assertEqual(answer(request("initialize", {"protocolVersion": NEWEST}), runtime)["result"]["protocolVersion"], NEWEST)

    def test_ids_zero_and_strings_are_echoed_unchanged(self):
        for id_ in (0, "abc", "", "7", 2**40, 1.5, -0.25):
            for message in (request("ping", id=id_), request("nonsense", id=id_), tool_call("nope", id=id_)):
                with self.subTest(id=id_, method=message["method"]):
                    reply = answer(message)
                    self.assertEqual(reply["id"], id_)
                    self.assertIs(type(reply["id"]), type(id_))


class ToolsListTest(unittest.TestCase):
    def tools(self):
        result = answer(request("tools/list"))["result"]
        self.assertNotIn("nextCursor", result)
        return result["tools"]

    def test_the_tools_match_the_operations_table_name_for_name(self):
        tools = self.tools()
        self.assertEqual([tool["name"] for tool in tools], [op.name for op in operations.OPERATIONS])
        for tool, op in zip(tools, operations.OPERATIONS):
            with self.subTest(tool=tool["name"]):
                self.assertEqual(tool["inputSchema"], json.loads(json.dumps(operations.input_schema(op))))
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIs(tool["inputSchema"]["additionalProperties"], False)
                self.assertTrue(tool["description"].strip())

    def test_permission_mode_is_not_offered(self):
        spawn = next(tool for tool in self.tools() if tool["name"] == "spawn")
        self.assertNotIn("permission_mode", spawn["inputSchema"]["properties"])

    def test_descriptions_carry_their_key_phrases(self):
        phrases = {
            "spawn": ["complete brief", "do not spawn again", "workspace not trusted", "from the main conversation only"],
            "stop": ["never your own", "`close` also deletes it", "on the devin cli stopping already deletes it"],
            "close": [
                "stop it, then delete it",
                "only when the owner asked you to close or remove it",
                "or told you to close the sessions you start once their work is done",
                "your own session is refused",
                "the harness decides what it refuses",
                "unpushed commits",
                "jam never forces a refusal",
                "`discard_unpushed`",
                "only when that commit is the head of a merged pull request or the owner said to drop it",
                "the session was stopped, but removing it failed",
            ],
            "whoami": [
                "stale",
                "run through bash, always has the current `session_id`",
                "after /clear its `id` is null",
                "if `id` is null, run `whoami` through bash, call `list`, and match its current `session_id`",
                "if no row matches, your short id is unknown, so do not guess it",
            ],
        }
        described = {tool["name"]: tool["description"].lower() for tool in self.tools()}
        for name, expected in phrases.items():
            for phrase in expected:
                with self.subTest(tool=name, phrase=phrase):
                    self.assertIn(phrase, described[name])


class ToolCallTest(unittest.TestCase):
    def test_a_successful_call_returns_one_text_content_holding_the_operation_json(self):
        harness = FakeHarness([session("aaaa1111")])
        reply = answer(tool_call("list", {"include_exited": True}), runtime_for(harness))
        is_error, text = tool_result(reply)
        self.assertFalse(is_error)
        self.assertEqual([row["id"] for row in json.loads(text)["sessions"]], ["aaaa1111"])
        self.assertEqual(harness.list_calls, [(True, None)])

    def test_every_verb_reaches_the_core(self):
        harness = FakeHarness([session("aaaa1111")])
        runtime = runtime_for(harness)
        self.assertEqual(json.loads(tool_result(answer(tool_call("spawn", {"cwd": "/work/x", "prompt": "do it", "name": "worker-c"}), runtime))[1])["id"], "cccc3333")
        self.assertEqual(json.loads(tool_result(answer(tool_call("stop", {"id": "aaaa1111"}), runtime))[1])["id"], "aaaa1111")
        # Not the caller's own session (`aaaa1111`, as whoami says), which close refuses.
        self.assertEqual(json.loads(tool_result(answer(tool_call("close", {"id": "bbbb2222"}), runtime))[1]), {"id": "bbbb2222", "closed": True})
        self.assertEqual(json.loads(tool_result(answer(tool_call("whoami", {}), runtime))[1]), {"session_id": SESSION_ID, "id": "aaaa1111"})
        self.assertEqual(harness.spawn_calls, [("/work/x", "do it", "worker-c", None)])
        self.assertEqual(harness.stop_calls, ["aaaa1111", "bbbb2222"])  # one for `stop`, one inside `close`
        self.assertEqual(harness.remove_calls, ["bbbb2222"])

    def test_arguments_may_be_left_out(self):
        for name in ("list", "whoami"):
            with self.subTest(name=name):
                self.assertFalse(tool_result(answer(tool_call(name)))[0])

    def test_failures_are_error_results_and_never_protocol_errors(self):
        broken = [
            ("a harness failure", tool_call("spawn", {"cwd": "/work/x", "prompt": "p"}), runtime_for(FakeHarness(spawn_error=HarnessError("boom"))), "boom"),
            ("a relative cwd", tool_call("spawn", {"cwd": "relative", "prompt": "p"}), runtime_for(), "cwd"),
            ("permission_mode", tool_call("spawn", {"cwd": "/work/x", "prompt": "p", "permission_mode": "default"}), runtime_for(), "unknown argument"),
            ("a missing key", tool_call("stop", {}), runtime_for(), "id"),
            ("a stop failure", tool_call("stop", {"id": "aaaa1111"}), runtime_for(FailingStopHarness()), "stop failed"),
            ("a close whose stop fails", tool_call("close", {"id": "aaaa1111"}), runtime_for(FailingStopHarness(), whoami=elsewhere), "stop failed"),
            ("a close of the caller's own session", tool_call("close", {"id": "aaaa1111"}), runtime_for(), "own session"),
            ("no identity", tool_call("whoami"), runtime_for(whoami=raising(HarnessError("not set"))), "not set"),
            ("a broken factory", tool_call("list"), mcp_runtime_with_broken_factory(), "internal error: RuntimeError"),
        ]
        for label, message, runtime, fragment in broken:
            with self.subTest(case=label):
                reply = answer(message, runtime)
                self.assertNotIn("error", reply)
                is_error, text = tool_result(reply)
                self.assertTrue(is_error)
                self.assertIn(fragment.lower(), text.lower())
                self.assertNotIn("/private/path", text)

    def test_a_lone_surrogate_escape_is_a_bad_argument_and_never_reaches_the_harness(self):
        # JSON allows "\ud800"; no subprocess can be given it, so it must not look like a started session.
        line = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"spawn","arguments":{"cwd":"/work/x","prompt":"hi \\ud800"}}}'
        harness = FakeHarness()
        is_error, text = tool_result(answer(line, runtime_for(harness)))
        self.assertTrue(is_error)
        self.assertIn("prompt", text)
        self.assertNotIn("internal error", text)
        self.assertNotIn("a session may have started", text)
        self.assertEqual(harness.spawn_calls, [])

    def test_malformed_calls_are_invalid_params(self):
        calls = [
            request("tools/call", {"name": "nope", "arguments": {}}),
            request("tools/call", {"name": "list", "arguments": []}),
            request("tools/call", {"name": "list", "arguments": "x"}),
            request("tools/call", {"name": "list", "arguments": 5}),
            request("tools/call", {"name": 5}),
            request("tools/call", {"name": ["list"]}),
            request("tools/call", {"name": None}),
            request("tools/call", {}),
            request("tools/call", []),
            request("tools/call", "list"),
            request("tools/call", 5),
            request("tools/call"),
        ]
        for message in calls:
            with self.subTest(params=message.get("params", "<absent>")):
                reply = answer(message)
                self.assertEqual(reply["error"]["code"], -32602)
                self.assertEqual(reply["id"], 1)

    def test_text_round_trips_through_the_wire_and_the_inner_json(self):
        odd = "/work/日本語\nline two end"
        harness = FakeHarness([Session(id="aaaa1111", session_id="s", name="名前\n二", cwd=odd, kind="background", state="done")])
        line = mcp.handle_line(runtime_for(harness), encoded(tool_call("list")), VERSION)
        self.assertTrue(line.isascii())
        self.assertNotIn("\n", line)
        text = tool_result(json.loads(line))[1]
        self.assertIn("日本語", text)
        self.assertEqual(json.loads(text)["sessions"][0]["cwd"], odd)
        self.assertEqual(json.loads(text)["sessions"][0]["name"], "名前\n二")
        spawn = FakeHarness()
        prompt = "日本語\n second line\n"
        answer(tool_call("spawn", {"cwd": odd, "prompt": prompt}), runtime_for(spawn))
        self.assertEqual(spawn.spawn_calls, [(odd, prompt, None, None)])


def mcp_runtime_with_broken_factory():
    return operations.Runtime(raising(RuntimeError("/private/path")), identity)


class EnvelopeTest(unittest.TestCase):
    PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def test_unparseable_json_and_invalid_utf8_are_parse_errors_with_a_null_id(self):
        for raw in (b"{not json", "{not json", b'{"jsonrpc":"2.0","id":1,"method":"ping","x":"\xff"}', b"\xff\xfe"):
            with self.subTest(raw=raw):
                reply = answer(raw)
                self.assertEqual(reply["error"]["code"], -32700)
                self.assertIn("id", reply)
                self.assertIsNone(reply["id"])

    def test_malformed_envelopes_are_invalid_requests_with_a_null_id(self):
        messages = [
            [self.PING],
            [],
            "hello",
            5,
            None,
            True,
            {"jsonrpc": "2.0", "id": 1, "method": 5},
            {"jsonrpc": "2.0", "id": 1, "method": ["ping"]},
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            {"jsonrpc": "2.0", "id": [1], "method": "ping"},
            {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"},
        ]
        for message in messages:
            with self.subTest(message=message):
                reply = answer(json.dumps(message))
                self.assertEqual(reply["error"]["code"], -32600)
                self.assertIsNone(reply["id"])

    def test_client_responses_and_blank_lines_are_ignored(self):
        ignored = [
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": "x", "error": {"code": -32601, "message": "no"}},
            b"",
            b"\n",
            b"  \r\n",
            "",
            "\t",
        ]
        for message in ignored:
            with self.subTest(message=message):
                self.assertIsNone(answer(message))

    def test_a_trailing_carriage_return_is_stripped(self):
        self.assertEqual(answer(json.dumps(self.PING).encode() + b"\r\n")["result"], {})
        self.assertEqual(answer(json.dumps(self.PING) + "\r\n")["result"], {})

    def test_a_fault_in_the_dispatcher_is_an_internal_error_without_detail(self):
        def boom(*args, **kwargs):
            raise RuntimeError("/private/path")

        # operations.call is the one collaborator the dispatcher cannot do without; make it
        # fail the way an unexpected bug would, whichever way mcp.py imports it.
        modules = [importlib.import_module("jam.operations"), importlib.import_module("jam.mcp")]
        with contextlib.ExitStack() as stack:
            for module in modules:
                if hasattr(module, "call"):
                    stack.enter_context(mock.patch.object(module, "call", boom))
            line = mcp.handle_line(runtime_for(), encoded(tool_call("list", id=3)), VERSION)
        reply = json.loads(line)
        self.assertEqual((reply["error"]["code"], reply["error"]["message"], reply["id"]), (-32603, "Internal error", 3))
        self.assertNotIn("private", line)


class ServeTest(unittest.TestCase):
    def test_every_written_line_is_single_line_ascii_json(self):
        lines = [json.dumps(request("initialize", {"protocolVersion": NEWEST})).encode(), json.dumps(tool_call("spawn", {"cwd": "/work/日本", "prompt": "改 行\n"})).encode()]
        written = serve_lines(runtime_for(), [line + b"\n" for line in lines])
        self.assertEqual(len(written), 2)
        for chunk in written:
            body = chunk.rstrip("\n")
            self.assertTrue(body.isascii())
            self.assertNotIn("\n", body)
            self.assertEqual(json.loads(body)["jsonrpc"], "2.0")

    def test_it_keeps_going_after_a_bad_line_and_ends_at_eof(self):
        lines = [b"{not json\n", b"\n", json.dumps(request("ping", id=2)) + "\n", b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n', json.dumps(request("ping", id=3)).encode()]
        replies = [json.loads(chunk) for chunk in serve_lines(runtime_for(), lines)]
        self.assertEqual([reply["id"] for reply in replies], [None, 2, 3])
        self.assertEqual(replies[0]["error"]["code"], -32700)

    def test_it_ends_quietly_on_a_broken_pipe(self):
        calls = []

        def broken(text):
            calls.append(text)
            raise BrokenPipeError()

        ping = json.dumps(request("ping")).encode() + b"\n"
        mcp.serve(runtime_for(), iter([ping, ping, ping]), broken, VERSION)
        self.assertEqual(len(calls), 1)

    def test_a_runtime_factory_that_raises_does_not_stop_the_server(self):
        runtime = mcp_runtime_with_broken_factory()
        lines = [json.dumps(tool_call("list", id=1)).encode(), json.dumps(request("ping", id=2)).encode()]
        first, second = [json.loads(chunk) for chunk in serve_lines(runtime, lines)]
        self.assertTrue(tool_result(first)[0])
        self.assertEqual(second["result"], {})


class NoIoTest(unittest.TestCase):
    """The platform may launch the server twice per session start, so startup must create nothing."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = self.root / "missing"

    def test_protocol_only_messages_touch_neither_the_disk_nor_the_harness(self):
        runner = FakeRunner()
        runtime = compose.build_runtime(str(self.data), {}, runner=runner)
        for message in (request("initialize", {"protocolVersion": NEWEST}), request("tools/list"), request("ping")):
            self.assertNotIn("error", answer(message, runtime))
        self.assertEqual(answer(request("server/discover"), runtime)["error"]["code"], -32601)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.data.exists())

    def test_listing_through_the_real_composition_creates_nothing_either(self):
        runtime = compose.build_runtime(str(self.data), {}, runner=FakeRunner(agents(LIVE)))
        is_error, text = tool_result(answer(tool_call("list"), runtime))
        self.assertFalse(is_error)
        self.assertEqual([row["id"] for row in json.loads(text)["sessions"]], ["aaaa1111"])
        self.assertFalse(self.data.exists())

    def test_a_failing_claude_binary_becomes_an_error_result(self):
        runtime = compose.build_runtime(str(self.data), {}, runner=FakeRunner(RunResult(3, "", "line one\nline two")))
        is_error, text = tool_result(answer(tool_call("list"), runtime))
        self.assertTrue(is_error)
        self.assertIn("line one line two", text)


if __name__ == "__main__":
    unittest.main()
