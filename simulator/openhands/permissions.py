"""Deterministic User-only approval policy for exact configured commands."""
CONTROL = {'session_state','request_transition','send_reply','accept_task','pause_task'}


def decide(actions, entries):
    commands = {entry['command'] for entry in entries}
    reasons=[]
    if not actions:
        reasons.append('No pending actions')
    for item in actions:
        name, action = item.get('tool_name'), item.get('action',{})
        if name in CONTROL:
            continue
        if name != 'terminal':
            reasons.append('Tool unavailable to User: '+str(name)+'. Ask Code for implementation information.')
        elif action.get('command') not in commands:
            reasons.append('Command is not a current-task run entry. Read session_state or ask Code.')
        elif action.get('is_input') or action.get('reset') is not True or action.get('timeout') is not None:
            reasons.append('Run entry requires reset=true, is_input=false, timeout=null for a fresh SDK shell.')
    return {'accepted':not reasons,'reasons':reasons}
