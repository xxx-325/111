"""Continue a verified clean SWE-Chain-Evo required-test setup failure."""
import argparse
import json
from pathlib import Path

from ..episode import load_environment
from .dialogue_export import export_dialogue
from .reviewed_episode import ReviewedEpisode, clone_evo_test_continuation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    args = parser.parse_args()
    load_environment(args.env_file)
    config = clone_evo_test_continuation(args.source, args.output)
    episode = ReviewedEpisode(config, args.output, resume=True)
    try:
        result = episode.run()
    finally:
        export_dialogue(args.output)
    print(json.dumps({'status': result}))


if __name__ == '__main__':
    main()
