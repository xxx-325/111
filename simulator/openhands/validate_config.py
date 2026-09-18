"""Static, no-model/no-Docker validation for OpenHands examples."""
import argparse
import json
from pathlib import Path

from .config import validate_paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path,
                        help="JSON files (default: examples/openhands*.json)")
    args = parser.parse_args(argv)
    paths = args.paths or sorted((Path(__file__).resolve().parents[2] / "examples").glob("openhands*.json"))
    checked, errors = validate_paths(paths)
    result = {"checked": checked, "errors": errors, "static": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
