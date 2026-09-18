"""Read-only probes of a completed/paused-at-boundary SDK execution container."""
import argparse
import json
import subprocess
from pathlib import Path

from ..episode import save

PROBE = '''import json, os, socket
from pathlib import Path
result = {"no_provider_key": not any(k in os.environ for k in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]),
          "no_docker_socket": not Path("/var/run/docker.sock").exists(),
          "no_host_private_mount": not Path("/private").exists(),
          "no_host_env": not Path("/workspace/.env").exists()}
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1).close()
    result["public_network_blocked"] = False
except OSError:
    result["public_network_blocked"] = True
result["config_has_no_key"] = "api_key" not in json.loads(Path("/inbox/config.json").read_text())
print(json.dumps(result))
'''


def probe(run):
    results = {}
    for role in ('user', 'code'):
        config = json.loads((run/'private'/role/'inbox/config.json').read_text())
        name = 'session-oh-'+config['conversation_id']
        info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
        if info['State']['Running']:
            raise ValueError('Probe only after an episode stopped; do not interfere with a live SDK')
        destinations = {m['Destination'] for m in info['Mounts']}
        expected = {'/workspace', '/sdk', '/inbox', '/outbox', '/mailbox', '/opt/runtime'}
        if destinations != expected or info['HostConfig']['NetworkMode'] != 'none':
            raise ValueError('unexpected mounts/network mode')
        subprocess.run(['docker', 'start', name], check=True, capture_output=True)
        try:
            observation = json.loads(subprocess.check_output(['docker', 'exec', name, 'python', '-c', PROBE]))
        finally:
            subprocess.run(['docker', 'stop', '-t', '2', name], check=True, capture_output=True)
        observation['mount_allowlist'] = True
        observation['source_and_other_role_not_mounted'] = all(
            Path(m['Source']).resolve() in {
                (run/('workspace' if role == 'code' else 'user-workspace')).resolve(),
                *((run/'private'/role/n).resolve() for n in ('sdk','inbox','outbox','mailbox','runtime')),
            } for m in info['Mounts'])
        results[role] = observation
    save(run/'isolation.json', {'kind': 'actual_container_probes', 'roles': results,
                              'passed': all(all(v.values()) for v in results.values())})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    print(json.dumps(probe(args.run.resolve())))
