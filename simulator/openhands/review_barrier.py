"""Host-only assistant review before a public handoff; never edits model text."""
import hashlib
import json
import time
from pathlib import Path
from ..episode import save


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def await_review(directory, identifier, material, deadline):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    digest=fingerprint(material)
    request=directory/(identifier+'.request.json')
    reply=directory/(identifier+'.decision.json')
    if request.exists():
        if json.loads(request.read_text())['sha256']!=digest:
            raise RuntimeError('Review input changed; no stale approval may be reused')
    else:
        save(request,dict(id=identifier,sha256=digest,material=material))
    print('AWAITING_REVIEW '+str(request),flush=True)
    while not reply.exists():
        if time.monotonic()>=deadline:
            raise TimeoutError('Assistant review not received; no automatic delivery')
        time.sleep(.2)
    decision=json.loads(reply.read_text())
    if (decision.get('sha256')!=digest or type(decision.get('approved')) is not bool
            or decision.get('reviewer')!='assistant' or not decision.get('reason')):
        raise ValueError('Review decision must match this exact material and include a reason')
    return decision
