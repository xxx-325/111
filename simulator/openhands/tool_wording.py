"""Role-specific SDK tool sets; implementations remain upstream-owned."""


def tool_specs(role, neutral_tools=True, candidate_pythonpath=None):
    from openhands.sdk.tool import Tool
    if role not in ('user', 'code', 'judge'):
        raise ValueError('unknown tool role')
    names = ({'user': [], 'judge': ['terminal', 'file_editor', 'task_tracker'],
              'code': ['terminal', 'file_editor', 'task_tracker']})[role]
    return [Tool(name=name, params=({'env': {'PYTHONPATH': candidate_pythonpath}}
                 if name == 'terminal' and candidate_pythonpath else {})) for name in names]
