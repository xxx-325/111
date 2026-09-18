"""Non-agent hidden checks in a third, isolated execution environment."""
import uuid
from pathlib import Path

from ..episode import clone_candidate, save
from ..sandbox import Sandbox


def verify(candidate, specification, private, image, timeout, revision):
    checks = specification.get('checks', [])
    if not checks:
        return [{'id': str(uuid.uuid4()), 'revision': revision, 'result': 'unknown',
                 'tool': 'isolated_verifier', 'summary': 'No configured executable acceptance evidence. Reference source was not compared line by line.'}]
    records = []
    for check in checks:
        location = private/('verification-'+uuid.uuid4().hex)
        clone_candidate(candidate, location/'candidate')
        if check.get('files'):
            clone_candidate(Path(check['files']).resolve(), location/'checks')
        raw = Sandbox(location, image, timeout).execute(check['command'])
        save(location/'result.json', raw)
        # No reference source, test source, stack traces or raw logs cross this boundary.
        records.append(dict(id=str(uuid.uuid4()), revision=revision, tool='isolated_verifier',
                            result='passed' if raw['exit_code'] == 0 else 'failed',
                            summary=check.get('public_name', 'Configured behavior check'), exit_code=raw['exit_code']))
    return records
