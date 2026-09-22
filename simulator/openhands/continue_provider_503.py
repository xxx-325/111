"""Continue a Code turn after provider 503 or discarded output truncation.

Usage:
    python -m simulator.openhands.continue_provider_503 \
        --source runs/<failed run> --output runs/<new run> --env-file .env

Unlike the other continuation entries this one keeps the interrupted Code turn
in flight. The new run directory gets a source-linked successor command id, the
persisted Code conversation is resumed without a new User message, and every
tool call that already completed stays completed.
"""
import argparse
import json
from pathlib import Path

from ..episode import load_environment
from .dialogue_export import export_dialogue
from .progressive import ProgressiveEpisode
from .provider_503_continuation import clone_provider_503_continuation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True,
                        help='Run directory that retained a 503 or output truncation')
    parser.add_argument('--output', type=Path, required=True,
                        help='New run directory for the continuation')
    parser.add_argument('--env-file', type=Path, default=Path('.env'),
                        help='Literal dotenv file; never printed or copied into containers')
    args = parser.parse_args()
    load_environment(args.env_file)
    config = clone_provider_503_continuation(args.source, args.output)
    episode = ProgressiveEpisode(config, args.output, resume=True)
    try:
        result = episode.run()
    finally:
        export_dialogue(args.output)
    print(json.dumps({'status': result}))


if __name__ == '__main__':
    main()
