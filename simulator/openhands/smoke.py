"""Real SDK compatibility gate. Fixtures are not episode quality evidence."""
import argparse
import json
import subprocess
import time
from pathlib import Path

from ..episode import load_environment, save
from .container import SDKContainer
from .relay import append


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', default='local/session-openhands:1.47.0')
    args = parser.parse_args()
    load_environment(args.env_file)
    config = json.loads(args.config.read_text())['user']
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    workspace = root / 'workspace'
    (workspace / 'candidate').mkdir(parents=True)
    # Deliberately tiny owned web fixture; no user browser/tab is accessed.
    (workspace / 'candidate/index.html').write_text('<!doctype html><button onclick="document.getElementById(\'result\').textContent=\'clicked-731\'">Run check</button><p id="result">ready</p>')
    accepted = []
    def control(value):
        append(root / 'control.jsonl', value)
        if value['operation'] == 'send':
            accepted.append(value['payload'])
            return {'accepted': True, 'handoff': True}
        return {'accepted': True, 'smoke_fixture': True, 'task_id': 'smoke', 'permit_id': 'smoke-permit'}
    system = 'You are testing SDK capabilities in an owned fixture. Use actual tools and report observed outcomes. Use request_transition (task_id=smoke), then send_reply with the returned permit_id for the final handoff.'
    deadline = time.monotonic()+1200
    def make_worker():
        return SDKContainer(root / 'private/agent', workspace, config, args.image, 'user',
                            system, deadline, control=control, browser=True, condenser_max_size=12)
    worker = make_worker()
    report = {'kind': 'framework_smoke_fixture', 'sdk': '1.47.0', 'passed': False}
    try:
        worker.start()
        worker.turn('Use file_editor to create probe.txt containing SDK-731, then use terminal to read it and run python -c "print(7*13)". Start python -m http.server 8000 in the workspace in the background. Use the browser tools to open http://127.0.0.1:8000, click Run check, and read the resulting page. Finally request_transition for task_id smoke with control CONTINUE, then send_reply reporting only your actual results. Remember the marker SDK-731 for the next turn.')
        first = worker.events()
        worker.turn('Without overwriting probe.txt, read it and confirm the previous browser click result from your history. Request a transition then send_reply.', condense=True)
        worker.close()
        worker = make_worker()
        worker.start(resume=True)
        worker.turn('This is the same conversation after a process restart. What marker did we create and what did the button display? Check the existing file without overwriting it, then request a transition and send_reply.')
        events = worker.events()
        names = sorted({e.get('tool_name') for e in events if e.get('tool_name')})
        report.update(tool_names=names, events=len(events), first_events=len(first), handoffs=len(accepted),
                      condensation_events=[e['kind'] for e in events if 'condens' in e.get('kind', '').lower()],
                      file_content=(workspace/'candidate/probe.txt').read_text(),
                      image_id=subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True).strip())
        report['candidate_pass'] = all(name in names for name in ['terminal', 'file_editor', 'browser_navigate', 'browser_click', 'send_reply']) and len(accepted) == 3 and bool(report['condensation_events'])
        report['review_required'] = 'Inspect actual browser observations, recovery and handoff results before marking passed.'
        save(root/'report.json', report)
        print(json.dumps(report, ensure_ascii=False))
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
        save(root/'report.json', report)
        raise
    finally:
        worker.close()


if __name__ == '__main__':
    main()
