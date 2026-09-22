from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol
from urllib import request


class Provider(Protocol):
    def complete(self, prompt: str, *, cwd: Path | None = None) -> str: ...


class OpenAICompatibleProvider:
    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete(self, prompt: str, *, cwd: Path | None = None) -> str:
        del cwd
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode()
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "curl/8.0",
            },
        )
        with request.urlopen(req, timeout=180) as response:
            data = json.loads(response.read())
        return data["choices"][0]["message"]["content"]


class CommandProvider:
    """Run a local agent CLI in the isolated repository.

    The command template receives only a prompt-file path through `{prompt}`.
    The prompt file contains the visible user request, never hidden targets.
    """

    def __init__(self, command_template: str, blocked_env: set[str] | None = None) -> None:
        self.command_template = command_template
        self.blocked_env = blocked_env or set()

    def complete(self, prompt: str, *, cwd: Path | None = None) -> str:
        if cwd is None:
            raise ValueError("command agents require a working directory")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(prompt)
            prompt_path = handle.name
        try:
            command = self.command_template.format(prompt=prompt_path)
            env = os.environ.copy()
            for name in self.blocked_env:
                env.pop(name, None)
            proc = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, timeout=1800, env=env)
            output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
            if proc.returncode:
                raise RuntimeError(f"agent command failed ({proc.returncode}): {output[-4000:]}")
            return output
        finally:
            Path(prompt_path).unlink(missing_ok=True)
