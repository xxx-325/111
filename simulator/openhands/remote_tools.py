"""SDK tool schemas backed only by a host-key-pinned SSH sandbox."""

import asyncio, base64, hashlib, json, re, shlex, threading, time, uuid
from pathlib import Path
import asyncssh
from openhands.sdk.tool import Tool, ToolAnnotations, ToolExecutor, register_tool
from openhands.tools.terminal.definition import TerminalTool, TerminalObservation
from openhands.tools.terminal.metadata import CmdOutputMetadata
from openhands.tools.file_editor.definition import (
    TOOL_DESCRIPTION,
    FileEditorAction,
    FileEditorTool,
    FileEditorObservation,
)
from .remote_safety import StopOnUncertainExecution


class SSH:
    def __init__(self, host, port, key, known):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        async def start():
            pinned = ([asyncssh.import_public_key(known)], [], [])
            c = await asyncssh.connect(
                host,
                port=port,
                username="sandbox",
                client_keys=[key],
                known_hosts=pinned,
            )
            return c, await c.start_sftp_client()

        self.conn, self.sftp = self.call(start())

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def run(self, cmd):
        return self.call(self.conn.run(cmd, check=False))

    def close(self):
        async def stop():
            self.conn.close()
            await self.conn.wait_closed()

        self.call(stop())
        self.loop.call_soon_threadsafe(self.loop.stop)


class RemoteTerminalExecutor(ToolExecutor):
    def __init__(self, ssh, state_dir, session, env):
        self.ssh, self.session = ssh, session
        self.env = dict(env)
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.state = root / (session + ".json")
        self.identity = root / (session + "-identity.json")
        self.output = "/workspace/tool-output/" + session
        self.log = self.output + "/terminal.log"
        ssh.run("mkdir -p " + shlex.quote(self.output))
        created = self._start()
        if created:
            self._configure()

    def _configure(self):
        exports = " ".join(f"export {k}={shlex.quote(v)};" for k, v in self.env.items())
        if exports:
            self.ssh.run(
                f"tmux send-keys -t {shlex.quote(self.session)} -l {shlex.quote(exports)}; tmux send-keys -t {shlex.quote(self.session)} Enter"
            )
        if self.session == "openhands-judge":
            self.ssh.run(
                f"tmux send-keys -t {shlex.quote(self.session)} -l "
                + shlex.quote("set -o pipefail")
                + f"; tmux send-keys -t {shlex.quote(self.session)} Enter"
            )
        self.ssh.run(
            f"tmux send-keys -t {shlex.quote(self.session)} -l "
            + shlex.quote("stty -echo; export PS1=")
            + f"; tmux send-keys -t {shlex.quote(self.session)} Enter"
        )

    async def _write_remote(self, path, data):
        async with self.ssh.sftp.open(path, "wb") as stream:
            await stream.write(data)

    async def _read_remote(self, path, offset=0):
        async with self.ssh.sftp.open(path, "rb") as stream:
            return await stream.read(-1, offset)

    def _clip(self, text):
        limit = 50_000
        if len(text) <= limit:
            return text
        path = self.output + "/full-" + uuid.uuid4().hex + ".log"
        self.ssh.call(self._write_remote(path, text.encode()))
        return (
            text[: limit // 2]
            + f"\n[output clipped; full output: {path}]\n"
            + text[-limit // 2 :]
        )

    def _start(self):
        actual = self.ssh.run(
            f"tmux display-message -p -t {shlex.quote(self.session)} '#{{session_id}}:#{{pane_id}}'"
        )
        if self.identity.exists():
            expected = json.loads(self.identity.read_text())["identity"]
            if actual.exit_status or actual.stdout.strip() != expected:
                raise RuntimeError("remote terminal identity is missing or changed")
            created = False
        else:
            if actual.exit_status == 0:
                raise RuntimeError("unrecognized remote terminal session")
            made = self.ssh.run(
                f"tmux new-session -d -s {shlex.quote(self.session)} -c /workspace/candidate"
            )
            if made.exit_status:
                raise RuntimeError("remote terminal creation failed")
            actual = self.ssh.run(
                f"tmux display-message -p -t {shlex.quote(self.session)} '#{{session_id}}:#{{pane_id}}'"
            )
            self.identity.write_text(json.dumps({"identity": actual.stdout.strip()}))
            created = True
        self.ssh.run(
            f"tmux pipe-pane -t {shlex.quote(self.session)}; "
            f"tmux pipe-pane -t {shlex.quote(self.session)} "
            + shlex.quote("cat >> " + self.log)
        )
        return created

    def read(self, n):
        size = int(
            self.ssh.run(
                f"wc -c < {shlex.quote(self.log)} 2>/dev/null || echo 0"
            ).stdout.strip()
            or 0
        )
        if n > size:
            raise RuntimeError("remote terminal output was truncated or replaced")
        return self.ssh.call(self._read_remote(self.log, n))

    @staticmethod
    def _split_marker(data, marker):
        marker_bytes = marker.encode()
        pattern = re.compile(
            rb"(?:^|\r?\n)" + re.escape(marker_bytes) + rb"(-?\d+)__(?:\r?\n|$)"
        )
        match = pattern.search(data)
        if match is None:
            return None
        return data[: match.start()] + data[match.end() :], int(match.group(1))

    @staticmethod
    def _safe_timeout_prefix(data, marker):
        """Withhold a possible partial completion marker until the next read."""
        marker_bytes = marker.encode()
        starts = (b"\r\n" + marker_bytes, b"\n" + marker_bytes, marker_bytes)
        for start in starts:
            index = data.rfind(start)
            if index >= 0 and (start != marker_bytes or index == 0):
                return data[:index], len(data) - index
        for length in range(min(len(data), max(map(len, starts))), 0, -1):
            suffix = data[-length:]
            if any(start.startswith(suffix) for start in starts):
                return data[:-length], length
        return data, 0

    def poll(self, r, limit):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            done = self.ssh.run("cat " + shlex.quote(r["status"]) + " 2>/dev/null")
            if done.exit_status == 0 and done.stdout.strip():
                status_parts = done.stdout.split()
                if len(status_parts) != 2 or status_parts[1] != "0":
                    raise RuntimeError(
                        "terminal completion marker could not be emitted"
                    )
                data = self.read(r["offset"])
                split = self._split_marker(data, r["marker"])
                if split is not None:
                    output, marker_code = split
                    status_code = int(status_parts[0])
                    if marker_code != status_code:
                        raise RuntimeError("terminal completion status mismatch")
                    self.state.unlink(missing_ok=True)
                    return output.decode(errors="replace"), status_code, False
            time.sleep(0.1)
        data = self.read(r["offset"])
        output, withheld = self._safe_timeout_prefix(data, r["marker"])
        r["offset"] += len(data) - withheld
        self.state.write_text(json.dumps(r))
        return output.decode(errors="replace"), -1, True

    def working_dir(self):
        result = self.ssh.run(
            f"tmux display-message -p -t {shlex.quote(self.session)} '#{{pane_current_path}}'"
        )
        if result.exit_status:
            raise RuntimeError("cannot read remote terminal working directory")
        return result.stdout.strip()

    def __call__(self, a, conversation=None):
        if a.reset and a.is_input:
            return TerminalObservation.from_text(
                text="Cannot use reset=True with is_input=True",
                command=a.command,
                is_error=True,
                metadata=CmdOutputMetadata(exit_code=-1),
            )
        reset = a.reset
        if a.reset:
            self.ssh.run(
                f"tmux kill-session -t {shlex.quote(self.session)} 2>/dev/null || true"
            )
            self.state.unlink(missing_ok=True)
            self.identity.unlink(missing_ok=True)
            self._start()
            self._configure()
            if not a.command:
                return TerminalObservation.from_text(
                    text="Terminal reset.",
                    command=a.command,
                    metadata=CmdOutputMetadata(exit_code=0),
                )
        # The SDK also uses empty input actions to poll pending command output.
        if a.is_input and a.command:
            keys = {
                "ENTER": "Enter",
                "C-c": "C-c",
                "C-d": "C-d",
                "C-z": "C-z",
                "ESC": "Escape",
                "TAB": "Tab",
                "BS": "BSpace",
                "UP": "Up",
                "DOWN": "Down",
                "LEFT": "Left",
                "RIGHT": "Right",
                "HOME": "Home",
                "END": "End",
                "PGUP": "PPage",
                "PGDN": "NPage",
            }
            key = keys.get(a.command)
            if (
                key is None
                and len(a.command) == 3
                and a.command.startswith("C-")
                and a.command[2].isalpha()
            ):
                key = "C-" + a.command[2].lower()
            cmd = (
                f"tmux send-keys -t {shlex.quote(self.session)} {key}"
                if key
                else f"tmux send-keys -t {shlex.quote(self.session)} -l {shlex.quote(a.command)}"
            )
            r = self.ssh.run(cmd)
            return TerminalObservation.from_text(
                text=r.stderr,
                command=a.command,
                is_error=r.exit_status != 0,
                metadata=CmdOutputMetadata(exit_code=-1),
            )
        if self.state.exists():
            rec = json.loads(self.state.read_text())
            if a.command:
                return TerminalObservation.from_text(
                    text="Previous command still running; use empty command.",
                    command=a.command,
                    is_error=True,
                    metadata=CmdOutputMetadata(exit_code=-1),
                )
        else:
            if not a.command:
                return TerminalObservation.from_text(
                    text="No running command.",
                    command="",
                    is_error=True,
                    metadata=CmdOutputMetadata(exit_code=0),
                )
            size = self.ssh.run(
                f"wc -c < {shlex.quote(self.log)} 2>/dev/null || echo 0"
            )
            rec = {
                "offset": int(size.stdout.strip() or 0),
                "status": self.output + "/" + uuid.uuid4().hex + ".status",
                "marker": "__OH_DONE_" + uuid.uuid4().hex + "_",
            }
            self.state.write_text(json.dumps(rec))
            script_path = self.output + "/" + uuid.uuid4().hex + ".sh"
            self.ssh.call(self._write_remote(script_path, a.command.encode()))
            script = (
                f"exec 3>&1; source {shlex.quote(script_path)}; rc=$?; "
                f"printf '\\n{rec['marker']}%s__\\n' \"$rc\" >&3; marker_rc=$?; "
                f'printf \'%s %s\\n\' "$rc" "$marker_rc" > '
                f"{shlex.quote(rec['status'])}"
            )
            sent = self.ssh.run(
                f"tmux send-keys -t {shlex.quote(self.session)} -l {shlex.quote(script)}; tmux send-keys -t {shlex.quote(self.session)} Enter"
            )
            if sent.exit_status:
                raise RuntimeError("remote terminal send failed")
        text, code, timed = self.poll(rec, a.timeout if a.timeout is not None else 30)
        return TerminalObservation.from_text(
            text=("Terminal reset.\n\n" if reset else "") + self._clip(text),
            command=("[RESET] " if reset else "") + a.command,
            timeout=timed,
            metadata=CmdOutputMetadata(exit_code=code, working_dir=self.working_dir()),
        )

    def close(self):
        self.ssh.close()


class RemoteTerminalTool(TerminalTool):
    name = "terminal"

    @classmethod
    def create(cls, conv_state, **p):
        inner = RemoteTerminalExecutor(
            SSH(p["host"], p["port"], p["private_key"], p["known_host_key"]),
            p["state_dir"],
            p["session"],
            p.get("env", {}),
        )
        exe = StopOnUncertainExecution(inner, p["state_dir"])
        return [
            TerminalTool.create(conv_state, executor=exe)[0].model_copy(
                update={"executor": exe}
            )
        ]


class RemoteFileEditorExecutor(ToolExecutor):
    MAX_FILE_SIZE = 10 * 1024 * 1024

    def __init__(self, ssh, state_dir):
        self.ssh = ssh
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.output = "/workspace/tool-output/file-editor"
        self.ssh.run("mkdir -p " + shlex.quote(self.output))

    def call(self, c):
        return self.ssh.call(c)

    async def read(self, p):
        async with self.ssh.sftp.open(p, "rb") as f:
            return await f.read()

    async def write(self, p, d):
        async with self.ssh.sftp.open(p, "wb") as f:
            await f.write(d)

    async def listdir(self, p):
        return [entry.filename async for entry in self.ssh.sftp.scandir(p)]

    def require_writable_parent(self, path):
        parent = str(Path(path).parent).replace("\\", "/")
        info = self.call(self.ssh.sftp.statvfs(parent))
        if info.flags & 1:  # POSIX ST_RDONLY
            raise ValueError("The target filesystem is read-only")

    def clip(self, text):
        if len(text) <= 50_000:
            return text
        path = self.output + "/full-" + uuid.uuid4().hex + ".txt"
        self.call(self.write(path, text.encode()))
        return (
            text[:25_000]
            + f"\n[output clipped; full output: {path}]\n"
            + text[-25_000:]
        )

    def __call__(self, a, conversation=None):
        p = a.path
        h = self.root / ("file-" + hashlib.sha256(p.encode()).hexdigest() + ".json")
        rows = json.loads(h.read_text()) if h.exists() else []
        try:
            if a.command == "view":
                attrs = self.call(self.ssh.sftp.stat(p))
                if attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY:
                    text = "\n".join(self.call(self.listdir(p)))
                else:
                    if attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
                        raise ValueError("Path must be a regular file or directory")
                    if attrs.size is not None and attrs.size > self.MAX_FILE_SIZE:
                        raise ValueError("File exceeds the 10 MB editor limit")
                    data = self.call(self.read(p)).decode()
                    lines = data.splitlines()
                    start = a.view_range[0] if a.view_range else 1
                    end = (
                        None
                        if not a.view_range or a.view_range[1] == -1
                        else a.view_range[1]
                    )
                    lines = lines[start - 1 : end]
                    text = "\n".join(f"{i:6}\t{x}" for i, x in enumerate(lines, start))
                return FileEditorObservation.from_text(
                    text=self.clip(text), command="view", path=p
                )
            try:
                attrs = self.call(self.ssh.sftp.stat(p))
                if attrs.type != asyncssh.FILEXFER_TYPE_REGULAR:
                    raise ValueError("Path must be a regular file")
                if attrs.size is not None and attrs.size > self.MAX_FILE_SIZE:
                    raise ValueError("File exceeds the 10 MB editor limit")
                old = self.call(self.read(p)).decode()
                exists = True
            except (asyncssh.SFTPNoSuchFile, FileNotFoundError):
                old = None
                exists = False
            if a.command != "view":
                self.require_writable_parent(p)
            if a.command == "undo_edit":
                if not rows:
                    raise ValueError("No edit history")
                prior = rows[-1]
                new = None if prior is None else base64.b64decode(prior).decode()
                if new is None:
                    self.call(self.ssh.sftp.remove(p))
                else:
                    self.call(self.write(p, new.encode()))
                h.write_text(json.dumps(rows[:-1]))
            else:
                if a.command == "create":
                    if exists:
                        raise ValueError("File already exists")
                    new = a.file_text
                elif a.command == "str_replace":
                    if old is None:
                        raise ValueError("File does not exist")
                    if old.count(a.old_str) != 1:
                        raise ValueError("old_str must occur exactly once")
                    new = old.replace(a.old_str, a.new_str or "")
                elif a.command == "insert":
                    lines = old.split("\n")
                    lines.insert(a.insert_line, a.new_str)
                    new = "\n".join(lines)
                else:
                    raise ValueError("unknown command")
                next_rows = rows + [
                    None if old is None else base64.b64encode(old.encode()).decode()
                ]
                self.call(self.write(p, new.encode()))
                h.write_text(json.dumps(next_rows[-10:]))
            return FileEditorObservation.from_text(
                text="File updated successfully",
                command=a.command,
                path=p,
                prev_exist=exists,
                old_content=old,
                new_content=new,
            )
        except (asyncssh.SFTPNoSuchFile, asyncssh.SFTPPermissionDenied) as e:
            return FileEditorObservation.from_text(
                text=str(e), command=a.command, path=p, is_error=True
            )
        except (asyncssh.Error, OSError):
            raise
        except (ValueError, UnicodeError) as e:
            return FileEditorObservation.from_text(
                text=str(e), command=a.command, path=p, is_error=True
            )

    def close(self):
        self.ssh.close()


class RemoteFileEditorTool(FileEditorTool):
    name = "file_editor"

    @classmethod
    def create(cls, conv_state, **p):
        inner = RemoteFileEditorExecutor(
            SSH(p["host"], p["port"], p["private_key"], p["known_host_key"]),
            p["state_dir"],
        )
        exe = StopOnUncertainExecution(inner, p["state_dir"])
        description = (
            TOOL_DESCRIPTION
            + "\n\nYour current working directory is: /workspace/candidate"
        )
        return [
            cls(
                action_type=FileEditorAction,
                observation_type=FileEditorObservation,
                description=description,
                annotations=ToolAnnotations(
                    title="file_editor",
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                    openWorldHint=False,
                ),
                executor=exe,
            )
        ]


register_tool("terminal", RemoteTerminalTool)
register_tool("file_editor", RemoteFileEditorTool)


def remote_tool_specs(
    role, host, port, private_key, known_host_key, state_dir, candidate_pythonpath=None
):
    if role not in ("code", "judge"):
        raise ValueError("remote tools only for Code/Judge")
    common = dict(
        host=host,
        port=port,
        private_key=private_key,
        known_host_key=known_host_key,
        state_dir=state_dir,
    )
    env = {"PYTHONPATH": candidate_pythonpath} if candidate_pythonpath else {}
    return [
        Tool(
            name="terminal", params=dict(common, session="openhands-" + role, env=env)
        ),
        Tool(name="file_editor", params=common),
        Tool(name="task_tracker"),
    ]
