"""Request a durable, host-managed pause of a live OpenHands run."""

import argparse
import json
import os
import time
import uuid
from pathlib import Path


def submit(run, reason):
    run = Path(run).resolve()
    checkpoint = run / "private/checkpoint.json"
    if not checkpoint.exists():
        raise ValueError("run has no checkpoint")
    state = json.loads(checkpoint.read_text()).get("state", {})
    if state.get("status") != "running":
        raise ValueError("run is not active")
    request = {
        "schema": "host-pause-request-v1",
        "id": str(uuid.uuid4()),
        "reason": reason.strip(),
        "requested_at": time.time(),
    }
    if not request["reason"] or len(request["reason"]) > 1000:
        raise ValueError("pause reason must contain 1-1000 characters")
    path = run / "private/pause-request.json"
    temporary = path.with_name(f".{path.name}.{request['id']}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(request, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link publishes the complete inode atomically and fails rather
        # than replacing an existing operator request.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()
    return checkpoint, request


def wait_for_ack(checkpoint, request_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        saved = json.loads(checkpoint.read_text())
        pause = saved.get("host_pause", {})
        if pause.get("request_id") == request_id:
            freeze = pause.get("freeze", {})
            if freeze.get("status") == "paused":
                return pause
            if freeze.get("status") == "failed":
                raise RuntimeError(
                    "host recorded pause but one or more role freezes failed"
                )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    raise TimeoutError("host did not acknowledge pause; request retained for inspection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    checkpoint, request = submit(args.run, args.reason)
    pause = wait_for_ack(checkpoint, request["id"], max(0, args.timeout))
    print(json.dumps({"status": "paused", "pause": pause}, ensure_ascii=False))


if __name__ == "__main__":
    main()
