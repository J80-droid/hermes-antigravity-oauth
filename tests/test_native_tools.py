"""agy-native tool steps become Hermes tool calls; unmappable ones stay neutralized."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

plugin_dir = Path(__file__).resolve().parent.parent
if str(plugin_dir) not in sys.path:
    sys.path.insert(0, str(plugin_dir))

import native_tools  # noqa: E402
from client import AntigravityClient  # noqa: E402

TERMINAL_TOOL = [{"type": "function", "function": {"name": "terminal", "parameters": {"type": "object"}}}]


def _step(name, params, idx=2):
    return {"step_index": idx, "state": "ACTIVE", "step_type": "tool", "tool_name": name,
            "tool_info": {"name": name, "parameters": params}}


class TranslateTests(unittest.TestCase):
    def test_run_command_maps_to_terminal(self):
        call = native_tools.translate(_step("run_command", {"CommandLine": "echo hi", "Cwd": "/w"}), {"terminal"}, 0)
        self.assertEqual(call.function.name, "terminal")
        self.assertEqual(json.loads(call.function.arguments), {"command": "echo hi", "workdir": "/w"})
        self.assertEqual(call.id, "agy_2_run_command")

    def test_unavailable_hermes_tool_is_not_mapped(self):
        self.assertIsNone(native_tools.translate(_step("run_command", {"CommandLine": "x"}), {"read_file"}, 0))

    def test_unknown_native_tool_is_not_mapped(self):
        self.assertIsNone(native_tools.translate(_step("browser_click_element", {"x": 1}), {"terminal"}, 0))

    def test_missing_parameters_is_not_mapped(self):
        self.assertIsNone(native_tools.translate({"step_type": "tool", "tool_name": "run_command"}, {"terminal"}, 0))

    def test_view_file_and_search(self):
        c = native_tools.translate(_step("view_file", {"AbsolutePath": "/a.py", "StartLine": 5}), {"read_file"}, 0)
        self.assertEqual(json.loads(c.function.arguments), {"path": "/a.py", "offset": 5})
        c = native_tools.translate(_step("grep_search", {"Query": "foo", "SearchPath": "/r"}), {"search_files"}, 0)
        self.assertEqual(json.loads(c.function.arguments), {"pattern": "foo", "target": "content", "path": "/r"})

    def test_hermes_read_file_name_is_reissued(self):
        # agy sometimes emits the Hermes name read_file instead of its own view_file.
        c = native_tools.translate(_step("read_file", {"path": "/a.py", "offset": 5}), {"read_file"}, 0)
        self.assertIsNotNone(c)
        self.assertEqual(c.function.name, "read_file")
        self.assertEqual(json.loads(c.function.arguments), {"path": "/a.py", "offset": 5})
        c = native_tools.translate(_step("write_file", {"path": "/a.py", "content": "x"}), {"write_file"}, 1)
        self.assertEqual(c.function.name, "write_file")
        c = native_tools.translate(_step("terminal", {"command": "echo hi"}), {"terminal"}, 2)
        self.assertEqual(json.loads(c.function.arguments)["command"], "echo hi")


class StreamBridgeTests(unittest.TestCase):
    def _run(self, events, tools):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = AntigravityClient(cwd=tmp.name)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events] + [""] * 50
        proc.stdin = MagicMock()
        # Hermetic: never probe the host keyring (on Linux that probe itself goes through Popen).
        with patch("client.is_authenticated", return_value=True), \
                patch("subprocess.Popen", return_value=proc), patch.object(client, "_terminate_process") as term:
            chunks = list(client.chat.completions.create(model="claude-sonnet-4-6", tools=tools, stream=True,
                                                         messages=[{"role": "user", "content": "run it"}]))
        return chunks, term, proc

    def test_native_run_command_is_reissued_as_hermes_terminal_call(self):
        events = [{"event": "init", "conversation_id": "c"},
                  {"event": "step_update", "step_update": _step("run_command", {"CommandLine": "echo SOUL_42"})}]
        chunks, term, proc = self._run(events, TERMINAL_TOOL)
        term.assert_called_once_with(proc)  # agy never gets to execute it
        calls = [tc for ch in chunks for tc in (ch.choices[0].delta.tool_calls or [])]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "terminal")
        self.assertEqual(json.loads(calls[0].function.arguments)["command"], "echo SOUL_42")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "tool_calls")

    def test_unmappable_native_tool_still_neutralized_without_call(self):
        events = [{"event": "step_update", "step_update": _step("browser_click_element", {"x": 1})}]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = AntigravityClient(cwd=tmp.name)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events] + [""]
        proc.stdin = MagicMock()
        proc.stderr = io.StringIO("")
        with patch("client.is_authenticated", return_value=True), \
                patch("subprocess.Popen", return_value=proc), \
                patch.object(client, "_terminate_process") as term:
            with self.assertRaises(RuntimeError) as caught:
                list(client.chat.completions.create(
                    model="claude-sonnet-4-6", tools=TERMINAL_TOOL, stream=True,
                    messages=[{"role": "user", "content": "run it"}]))
        term.assert_called_once_with(proc)
        self.assertIn("browser_click_element", str(caught.exception))

    def test_self_killed_native_tool_names_the_tool_not_the_exit_code(self):
        # taskkill makes poll() return 1. That code is the kill, not agy's verdict.
        events = [{"event": "step_update", "step_update": _step("browser_click_element", {"x": 1})}]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = AntigravityClient(cwd=tmp.name)
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events] + [""]
        proc.stdin = MagicMock()
        proc.stderr = io.StringIO("")

        def _kill(p):
            p.poll.return_value = 1

        with patch("client.is_authenticated", return_value=True), \
                patch("subprocess.Popen", return_value=proc), \
                patch.object(client, "_terminate_process", side_effect=_kill):
            with self.assertRaises(RuntimeError) as caught:
                list(client.chat.completions.create(
                    model="gemini-4-argon",
                    tools=TERMINAL_TOOL,
                    stream=True,
                    messages=[{"role": "user", "content": "look"}],
                ))
        message = str(caught.exception)
        self.assertIn("browser_click_element", message)
        self.assertNotIn("return code", message)

    def test_worker_exit_reports_stderr_tail(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = AntigravityClient(cwd=tmp.name)
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stdout.readline.return_value = ""
        proc.stdin = MagicMock()
        proc.stderr = io.StringIO("agy: model gemini-4-argon-high rejected\n")
        with patch("client.is_authenticated", return_value=True), \
                patch("subprocess.Popen", return_value=proc), \
                patch.object(client, "_terminate_process"):
            with self.assertRaises(RuntimeError) as caught:
                list(client.chat.completions.create(
                    model="gemini-4-argon",
                    stream=True,
                    messages=[{"role": "user", "content": "look"}],
                ))
        message = str(caught.exception)
        self.assertIn("gemini-4-argon-high rejected", message)
        self.assertNotIn("return code", message)


if __name__ == "__main__":
    unittest.main()
