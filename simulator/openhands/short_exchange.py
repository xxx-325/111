"""One real Code turn between two turns of the same User conversation."""
import argparse
import json
from pathlib import Path
from ..episode import load_environment, save
from .episode import OpenHandsEpisode
from .role_diagnostic import FirstDelegation, report_page


class ShortExchange(FirstDelegation):
    def _control(self, packet):
        if self.state.data.get('code_reply') and packet.get('operation') == 'send':
            return super()._control(packet)
        result = OpenHandsEpisode._control(self, packet)
        return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    load_environment(args.env_file)
    config=json.loads(args.config.read_text())
    if len(config['tasks']) != 1:
        raise ValueError('Only one issue in short exchange')
    episode=ShortExchange(config,args.output,resume=args.resume)
    initial=episode.user_input()
    status=episode.run()
    report=dict(mode='short_exchange',status=status,input=initial,policy=episode.policy,
        draft=episode.saved.get('diagnostic_draft'),public=episode.saved['public'],
        pause_reason=episode.state.data.get('pause_reason'),assistant_review='pending',
        conversations={role:json.loads((agent.directory/'inbox/config.json').read_text())['conversation_id']
            for role,agent in episode.agents.items()},
        tools=[dict(role=role,tool=e.get('tool_name'),action=e.get('action'),observation=e.get('observation'))
            for role,agent in episode.agents.items() for e in agent.events() if e.get('kind') in ('ActionEvent','ObservationEvent')])
    save(args.output/'report.json',report)
    report_page(args.output,report)
    print(json.dumps({'status':status,'draft':bool(report['draft'])}))


if __name__=='__main__':
    main()
