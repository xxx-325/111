"""Verify a completed-run resume does not deliver messages or call the model again."""
import argparse
import hashlib
import json
from pathlib import Path

from ..episode import load_environment, save
from .episode import OpenHandsEpisode, USER_SYSTEM, CODE_SYSTEM
from .container import SDKContainer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    parser.add_argument('--env-file', type=Path, required=True)
    args = parser.parse_args()
    load_environment(args.env_file)
    run = args.run.resolve()
    checkpoint = json.loads((run/'private/checkpoint.json').read_text())
    if checkpoint['state']['phase'] != 'ended' or checkpoint.get('in_flight'):
        raise ValueError('This probe requires a completed safe boundary')
    before = hashlib.sha256((run/'session.jsonl').read_bytes()).hexdigest()
    calls = checkpoint['budget']['calls']
    episode = OpenHandsEpisode(checkpoint['config'], run, resume=True)
    loaded = []
    for role, system in [('code', CODE_SYSTEM), ('user', USER_SYSTEM)]:
        agent = SDKContainer(run/'private'/role, run/('workspace' if role == 'code' else 'user-workspace'),
            checkpoint['config'][role], episode.image, role, system, episode.budget.deadline,
            control=(lambda value: {'accepted': False, 'reason': 'read-only resume probe'}) if role == 'user' else None)
        try:
            agent.start(resume=True)
            loaded.append(role)
        finally:
            agent.close()
    result = episode.run()
    after = json.loads((run/'private/checkpoint.json').read_text())
    report = dict(kind='actual_completed_boundary_resume', status=result,
                  sdk_conversations_loaded=loaded,
                  public_session_unchanged=before == hashlib.sha256((run/'session.jsonl').read_bytes()).hexdigest(),
                  no_new_model_calls=after['budget']['calls'] == calls,
                  no_extra_acceptance=len(after['state']['accepted']) == len(checkpoint['state']['accepted']))
    save(run/'resume-check.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
