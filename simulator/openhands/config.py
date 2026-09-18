"""Pure validation for OpenHands example configuration files.

This module intentionally does not import the SDK or inspect Docker.  It is
used by the example check command before an online/model-backed run.
"""
import re
from pathlib import Path


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def validate_example(config, *, source="<config>"):
    """Return a short list of static configuration errors."""
    errors = []
    if not isinstance(config, dict):
        return [f"{source}: top level must be an object"]
    if config.get("runtime") != "openhands":
        errors.append(f"{source}: runtime must be 'openhands'")
    if config.get("execution_backend", "ssh_sandbox") != "ssh_sandbox":
        errors.append(f"{source}: execution_backend must be 'ssh_sandbox'")
    control = config.get("image")
    execution = config.get("execution_image")
    if not isinstance(control, str) or not _DIGEST.fullmatch(control):
        errors.append(f"{source}: image must be an immutable sha256 digest")
    if not isinstance(execution, str) or not _DIGEST.fullmatch(execution):
        errors.append(f"{source}: execution_image must be an immutable sha256 digest")
    if isinstance(control, str) and isinstance(execution, str) and control == execution:
        errors.append(f"{source}: image and execution_image must be different images")
    if config.get("browser") is not False:
        errors.append(f"{source}: browser must be false for ssh_sandbox episodes")
    tasks = config.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append(f"{source}: tasks must be a non-empty list")
    for role in ("user", "code"):
        value = config.get(role)
        if not isinstance(value, dict) or not value.get("model"):
            errors.append(f"{source}: {role}.model is required")
    return errors


def validate_paths(paths):
    """Validate JSON files and return ``(checked, errors)``."""
    import json

    errors = []
    checked = []
    for path in paths:
        path = Path(path)
        checked.append(str(path))
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            errors.append(f"{path}: cannot read JSON ({exc})")
            continue
        errors.extend(validate_example(config, source=str(path)))
    return checked, errors
