"""Bind a privately inferred commit requirement to its exact reference diff."""
import hashlib
import subprocess
from .source import project_commit
from .issue_stages import prepare_issue, InvalidDecomposition

VERSION = 'commit-requirement-v1'


def reference_patch(task, repo):
    return subprocess.check_output(['git', '-C', str(repo), 'diff',
                                    task['base'], task['reference']], text=True)


def validate_projection(record, task, repo):
    patch = reference_patch(task, repo)
    if (record.get('version') != VERSION or record.get('reference') != task['reference']
            or record.get('patch_sha256') != hashlib.sha256(patch.encode()).hexdigest()
            or record.get('review_passed') is not True):
        raise ValueError('commit requirement does not match the audited reference')
    document = record.get('document', {})
    if set(document) != {'title', 'body'} or any(not isinstance(v, str) or not v.strip() for v in document.values()):
        raise ValueError('invalid inferred requirement document')
    return document


def prepare_commit(relay, task, repo):
    patch = reference_patch(task, repo)
    document = project_commit(dict(task, patch=patch), relay)
    record = dict(version=VERSION, reference=task['reference'],
                  patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
                  document=document, review_passed=True)
    try:
        return dict(prepare_issue(relay, document), projection=record)
    except InvalidDecomposition as error:
        raise InvalidDecomposition(str(error), dict(projection=record, fragments=error.draft)) from error
