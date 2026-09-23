"""CLI for issue and legacy commit inputs."""
import argparse
import json
from pathlib import Path

from .episode import Episode, load_environment


def main():
    parser = argparse.ArgumentParser(description='Generate continuous issue dialogues in offline Docker workspaces.')
    parser.add_argument('--config', type=Path, help='JSON configuration; see examples/deepseek.json')
    parser.add_argument('--env-file', type=Path, help='Literal dotenv file; never printed or copied into containers')
    parser.add_argument('--output', type=Path, required=True, help='New run directory, or existing directory with --resume')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--repo', help='Legacy local repository or GitHub owner/name')
    parser.add_argument('--commits', help='Legacy comma-separated commit refs')
    parser.add_argument('--base')
    parser.add_argument('--image', default='python:3.12-slim')
    parser.add_argument('--max-rounds', type=int, default=8)
    for role in ('user', 'code'):
        parser.add_argument(f'--{role}-api-key-env', default='DEEPSEEK_API_KEY')
        parser.add_argument(f'--{role}-model', default='deepseek-v4-flash')
        parser.add_argument(f'--{role}-base-url', default='https://api.deepseek.com')
    args = parser.parse_args()
    load_environment(args.env_file)
    if args.config:
        config = json.loads(args.config.read_text())
    else:
        if not args.repo or not args.commits:
            parser.error('provide --config, or --repo and --commits')
        config = dict(repository=args.repo, base=args.base, tasks=[{'commit': c.strip()} for c in args.commits.split(',')], image=args.image, max_rounds=args.max_rounds)
        for role in ('user', 'code'):
            config[role] = dict(adapter='api', key_env=getattr(args, role + '_api_key_env'), model=getattr(args, role + '_model'), base_url=getattr(args, role + '_base_url'))
    if config.get('runtime') == 'openhands':
        from .openhands.episode import OpenHandsEpisode
        if config.get('progressive_issues'):
            from .openhands.progressive import ProgressiveEpisode
            result = ProgressiveEpisode(config, args.output, resume=args.resume).run()
        else:
            result = OpenHandsEpisode(config, args.output, resume=args.resume).run()
    elif config.get('runtime') in (None, 'legacy'):
        result = Episode(config, args.output, resume=args.resume).run()
    else:
        raise ValueError('unknown runtime; no automatic fallback')
    if result == 'completed' and config.get('runtime') == 'openhands' and config.get('scenario_file'):
        from .openhands.memory_episode import export_episode
        from .openhands.retention import compact_completed_run
        package = args.output.with_name(args.output.name + '-package')
        export_episode(args.output, package)
        compact_completed_run(args.output, package)
        print(json.dumps({'package': str(package), 'runtime_state_removed': True}))
    print(json.dumps({'status': result, 'output': str(args.output)}, ensure_ascii=False))
    if result != 'completed':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
