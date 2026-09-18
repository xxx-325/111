import asyncio
import json
import tempfile
import unittest
from pathlib import Path

try:
    import asyncssh
    from simulator.openhands.remote_tools import (
        remote_tool_specs,
        RemoteTerminalExecutor,
        RemoteFileEditorExecutor,
    )
    from openhands.tools.terminal.definition import TerminalAction
    from openhands.tools.file_editor.definition import FileEditorAction
except ImportError:
    remote_tool_specs = RemoteTerminalExecutor = RemoteFileEditorExecutor = None
    TerminalAction = FileEditorAction = None


@unittest.skipIf(
    remote_tool_specs is None, "AsyncSSH is installed in the control image"
)
class RemoteToolTests(unittest.TestCase):
    class MemoryFile:
        def __init__(self, files, path, mode):
            self.files, self.path, self.mode = files, path, mode

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def read(self, size=-1, offset=None):
            data = self.files[self.path]
            if offset is not None:
                data = data[offset:]
            return data if size == -1 else data[:size]

        async def write(self, data):
            self.files[self.path] = data

    class MemorySFTP:
        def __init__(self):
            self.files = {}

        def open(self, path, mode):
            if "r" in mode and path not in self.files:
                raise FileNotFoundError(path)
            return RemoteToolTests.MemoryFile(self.files, path, mode)

        async def remove(self, path):
            del self.files[path]

        async def stat(self, path):
            if path not in self.files and path != "/workspace/candidate":
                raise FileNotFoundError(path)
            return type(
                "Attrs",
                (),
                {
                    "type": 2 if path == "/workspace/candidate" else 1,
                    "size": (
                        None
                        if path == "/workspace/candidate"
                        else len(self.files[path])
                    ),
                },
            )()

        async def scandir(self, path):
            for name in ("a", "b"):
                yield type("Entry", (), {"filename": name})()

        async def statvfs(self, path):
            return asyncssh.SFTPVFSAttrs(flags=getattr(self, "flags", 0))

    class MemorySSH:
        def __init__(self):
            self.sftp = RemoteToolTests.MemorySFTP()

        def call(self, coro):
            return asyncio.run(coro)

        def run(self, _command):
            return type("Result", (), {"stdout": "", "stderr": "", "exit_status": 0})()

    def test_only_judge_shell_enables_pipefail(self):
        class Recorder:
            def __init__(self):
                self.commands = []

            def run(self, command):
                self.commands.append(command)

        for session, expected in (("openhands-judge", True), ("openhands-code", False)):
            executor = RemoteTerminalExecutor.__new__(RemoteTerminalExecutor)
            executor.ssh = Recorder()
            executor.session = session
            executor.env = {}
            executor._configure()
            self.assertEqual(
                any("pipefail" in command for command in executor.ssh.commands),
                expected,
            )

    def test_specs_keep_public_names_and_reject_user(self):
        specs = remote_tool_specs(
            "code",
            "sandbox",
            2222,
            "/key",
            "ssh-ed25519 AAAA",
            "/state",
            "/workspace/candidate/src",
        )
        self.assertEqual(
            [x.name for x in specs], ["terminal", "file_editor", "task_tracker"]
        )
        self.assertEqual(
            specs[0].params["env"], {"PYTHONPATH": "/workspace/candidate/src"}
        )
        with self.assertRaises(ValueError):
            remote_tool_specs("user", "h", 1, "k", "p", "s")

    def test_timeout_is_persisted_and_empty_command_only_continues(self):
        class Result:
            def __init__(self, out="", code=0):
                self.stdout, self.stderr, self.exit_status = out, "", code

        class FakeSSH:
            def __init__(self):
                self.status = False
                self.sent = []
                self.sftp = RemoteToolTests.MemorySFTP()
                self.session = False

            def call(self, coro):
                return asyncio.run(coro)

            def run(self, cmd):
                self.sent.append(cmd)
                if cmd.startswith("tmux display-message"):
                    if "pane_current_path" in cmd:
                        return Result("/workspace/candidate\n")
                    return Result("$1:%1\n", 0) if self.session else Result("", 1)
                if cmd.startswith("tmux new-session"):
                    self.session = True
                    return Result()
                if cmd.startswith("tmux kill-session"):
                    self.session = False
                    return Result()
                if cmd.startswith("wc -c"):
                    log = self.sftp.files.get(
                        "/workspace/tool-output/session/terminal.log", b""
                    )
                    return Result(f"{len(log)}\n")
                if cmd.startswith("cat "):
                    return Result("0 0\n", 0) if self.status else Result("", 1)
                return Result()

        with tempfile.TemporaryDirectory() as d:
            ssh = FakeSSH()
            exe = RemoteTerminalExecutor(ssh, d, "session", {})
            ssh.sftp.files[exe.log] = b"partial\n"
            first = exe(TerminalAction(command="sleep 2", timeout=0))
            self.assertTrue(first.timeout)
            self.assertTrue(exe.state.exists())
            before = sum("send-keys" in x and "sleep 2" in x for x in ssh.sent)
            ssh.status = True
            record = json.loads(exe.state.read_text())
            ssh.sftp.files[exe.log] += ("\n" + record["marker"] + "0__\n").encode()
            second = exe(TerminalAction(command="", timeout=1))
            after = sum("send-keys" in x and "sleep 2" in x for x in ssh.sent)
            self.assertEqual(before, after)
            self.assertEqual(second.metadata.exit_code, 0)
            self.assertFalse(exe.state.exists())

    def test_reset_with_command_recreates_then_executes(self):
        class Result:
            def __init__(self, out="", code=0):
                self.stdout, self.stderr, self.exit_status = out, "", code

        class FakeSSH:
            def __init__(self):
                self.sftp = RemoteToolTests.MemorySFTP()
                self.session = False
                self.sent = []

            def call(self, coro):
                return asyncio.run(coro)

            def run(self, cmd):
                self.sent.append(cmd)
                if cmd.startswith("tmux display-message"):
                    if "pane_current_path" in cmd:
                        return Result("/workspace/candidate\n")
                    return Result("$1:%1\n") if self.session else Result("", 1)
                if cmd.startswith("tmux new-session"):
                    self.session = True
                    return Result()
                if cmd.startswith("tmux kill-session"):
                    self.session = False
                    return Result()
                if cmd.startswith("wc -c"):
                    return Result("5\n")
                if cmd.startswith("cat "):
                    return Result("0 0\n")
                return Result()

        with tempfile.TemporaryDirectory() as d:
            ssh = FakeSSH()
            exe = RemoteTerminalExecutor(ssh, d, "session", {"X": "kept"})
            exe.poll = lambda _record, _limit: ("done\n", 0, False)
            result = exe(TerminalAction(command="pwd", reset=True, timeout=1))
            self.assertEqual(result.metadata.exit_code, 0)
            self.assertEqual(result.command, "[RESET] pwd")
            self.assertIn("Terminal reset.", result.text)
            self.assertIn(b"pwd", ssh.sftp.files.values())
            self.assertGreaterEqual(
                sum("export X=" in command for command in ssh.sent), 2
            )

    def test_completion_waits_for_marker_after_status(self):
        class Result:
            stdout = "0 0\n"
            stderr = ""
            exit_status = 0

        class FakeSSH:
            def run(self, _command):
                return Result()

        with tempfile.TemporaryDirectory() as d:
            exe = RemoteTerminalExecutor.__new__(RemoteTerminalExecutor)
            exe.ssh = FakeSSH()
            exe.state = Path(d) / "state.json"
            exe.state.write_text("{}")
            reads = iter((b"ordinary output", b"ordinary output\n__OH_DONE_x_0__\n"))
            exe.read = lambda _offset: next(reads)
            output, code, timed = exe.poll(
                {"offset": 0, "status": "/status", "marker": "__OH_DONE_x_"},
                1,
            )
            self.assertEqual(output, "ordinary output")
            self.assertEqual(code, 0)
            self.assertFalse(timed)

    def test_partial_marker_is_withheld_and_reassembled(self):
        marker = "__OH_DONE_chunked_"
        safe, withheld = RemoteTerminalExecutor._safe_timeout_prefix(
            b"output\n__OH_DONE_ch", marker
        )
        self.assertEqual(safe, b"output")
        self.assertEqual(withheld, len(b"\n__OH_DONE_ch"))
        split = RemoteTerminalExecutor._split_marker(
            b"\n__OH_DONE_chunked_7__\n", marker
        )
        self.assertEqual(split, (b"", 7))

        for pending in (
            b"output\n__OH_DONE_chunked_",
            b"output\n__OH_DONE_chunked_7_",
            b"output\n__OH_DONE_chunked_7__\n",
            b"output\r\n__OH_DONE_chunked_7__\r\n",
        ):
            safe, withheld = RemoteTerminalExecutor._safe_timeout_prefix(
                pending, marker
            )
            self.assertEqual(safe, b"output")
            self.assertEqual(withheld, len(pending) - len(safe))

    def test_marker_and_status_exit_code_mismatch_is_fatal(self):
        class Result:
            stdout = "1 0\n"
            stderr = ""
            exit_status = 0

        class FakeSSH:
            def run(self, _command):
                return Result()

        with tempfile.TemporaryDirectory() as d:
            exe = RemoteTerminalExecutor.__new__(RemoteTerminalExecutor)
            exe.ssh = FakeSSH()
            exe.state = Path(d) / "state.json"
            exe.state.write_text("{}")
            exe.read = lambda _offset: b"\n__OH_DONE_x_0__\n"
            with self.assertRaisesRegex(RuntimeError, "status mismatch"):
                exe.poll(
                    {
                        "offset": 0,
                        "status": "/status",
                        "marker": "__OH_DONE_x_",
                    },
                    1,
                )

    def test_reset_with_input_is_normal_error(self):
        exe = RemoteTerminalExecutor.__new__(RemoteTerminalExecutor)
        result = exe(TerminalAction(command="hello", reset=True, is_input=True))
        self.assertTrue(result.is_error)
        self.assertIn("Cannot use reset", result.text)

    def test_restore_requires_same_tmux_identity_and_lost_output_fails(self):
        with tempfile.TemporaryDirectory() as d:
            # Reuse the focused fake from the timeout test through a small equivalent.
            class Result:
                def __init__(self, out="", code=0):
                    self.stdout, self.stderr, self.exit_status = out, "", code

            class Fake:
                def __init__(self):
                    self.identity = "$1:%1"
                    self.session = False
                    self.sftp = RemoteToolTests.MemorySFTP()

                def call(self, coro):
                    return asyncio.run(coro)

                def run(self, cmd):
                    if cmd.startswith("tmux display-message"):
                        return (
                            Result(self.identity + "\n")
                            if self.session
                            else Result("", 1)
                        )
                    if cmd.startswith("tmux new-session"):
                        self.session = True
                        return Result()
                    if cmd.startswith("wc -c"):
                        return Result("0\n")
                    return Result()

            ssh = Fake()
            first = RemoteTerminalExecutor(ssh, d, "session", {})
            RemoteTerminalExecutor(ssh, d, "session", {})
            ssh.identity = "$2:%2"
            with self.assertRaises(RuntimeError):
                RemoteTerminalExecutor(ssh, d, "session", {})
            with self.assertRaises(RuntimeError):
                first.read(1)

    def test_file_edits_are_atomic_support_delete_and_undo(self):
        with tempfile.TemporaryDirectory() as d:
            ssh = self.MemorySSH()
            ssh.sftp.files["/workspace/candidate/a"] = b"one\ntwo"
            exe = RemoteFileEditorExecutor(ssh, d)
            changed = exe(
                FileEditorAction(
                    command="str_replace",
                    path="/workspace/candidate/a",
                    old_str="one",
                    new_str=None,
                )
            )
            self.assertFalse(changed.is_error)
            self.assertEqual(ssh.sftp.files["/workspace/candidate/a"], b"\ntwo")
            exe(
                FileEditorAction(
                    command="insert",
                    path="/workspace/candidate/a",
                    insert_line=1,
                    new_str="middle",
                )
            )
            self.assertEqual(ssh.sftp.files["/workspace/candidate/a"], b"\nmiddle\ntwo")
            undone = exe(
                FileEditorAction(command="undo_edit", path="/workspace/candidate/a")
            )
            self.assertFalse(undone.is_error)
            self.assertEqual(ssh.sftp.files["/workspace/candidate/a"], b"\ntwo")

    def test_directory_view_consumes_async_sftp_iterator(self):
        with tempfile.TemporaryDirectory() as d:
            exe = RemoteFileEditorExecutor(self.MemorySSH(), d)
            viewed = exe(FileEditorAction(command="view", path="/workspace/candidate"))
            self.assertFalse(viewed.is_error)
            self.assertEqual(viewed.text, "a\nb")

    def test_failed_remote_write_does_not_commit_history(self):
        with tempfile.TemporaryDirectory() as d:
            ssh = self.MemorySSH()
            ssh.sftp.files["/workspace/candidate/a"] = b"old"
            exe = RemoteFileEditorExecutor(ssh, d)

            async def fail(*_):
                raise OSError("transport lost")

            exe.write = fail
            with self.assertRaises(OSError):
                exe(
                    FileEditorAction(
                        command="str_replace",
                        path="/workspace/candidate/a",
                        old_str="old",
                        new_str="new",
                    )
                )
            self.assertEqual(list(Path(d).glob("file-*.json")), [])

    def test_readonly_filesystem_is_a_normal_preflight_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            ssh = self.MemorySSH()
            ssh.sftp.flags = 1
            exe = RemoteFileEditorExecutor(ssh, d)
            result = exe(
                FileEditorAction(
                    command="create", path="/workspace/candidate/no", file_text="x"
                )
            )
            self.assertTrue(result.is_error)
            self.assertIn("read-only", result.text)
            self.assertNotIn("/workspace/candidate/no", ssh.sftp.files)

    def test_replace_missing_file_is_a_normal_input_error(self):
        with tempfile.TemporaryDirectory() as d:
            exe = RemoteFileEditorExecutor(self.MemorySSH(), d)
            result = exe(
                FileEditorAction(
                    command="str_replace",
                    path="/workspace/candidate/missing",
                    old_str="old",
                    new_str="new",
                )
            )
            self.assertTrue(result.is_error)
            self.assertIn("does not exist", result.text)


if __name__ == "__main__":
    unittest.main()
