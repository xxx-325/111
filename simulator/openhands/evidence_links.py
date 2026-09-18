"""Recover omitted references from exact quoted observation text, not commands."""
def quoted_observations(text, checks):
    matches = []
    for check in checks:
        experience = check.get('simulated_experience') or {}
        observation = check.get('observation') or experience.get('observation') or {}
        if not isinstance(observation, dict):
            continue
        lines = []
        for block in observation.get('content', []):
            if block.get('type') != 'text':
                continue
            for line in block.get('text', '').splitlines():
                line = line.strip()
                if len(line) >= 24 and line in text:
                    lines.append(line)
        for key in ('input', 'output', 'error', 'summary'):
            value = observation.get(key)
            if isinstance(value, str) and len(value.strip()) >= 24 and value in text:
                lines.append(value)
        if lines:
            matches.append({'id':check['id'], 'quoted_lines':list(dict.fromkeys(lines))})
    return matches
