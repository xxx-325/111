"""Select current requirements from PR prose, retaining exact source evidence."""
from .issue_stages import call_json, InvalidDecomposition, prepare_issue

VERSION = 'requirement-scope-v1'
SYSTEM = '''从 PR 正文选取本次用户需要的能力、行为约束和必要技术线索，返回 JSON {quotes:[原文片段]}。
逐字引用，不改写。排除贡献模板、已执行的检查报告、关联编号、历史完成叙述和未来设想；本次兼容约束要保留。不要读取链接或补充实现。'''
AUDIT = '''审核选取内容是否准确界定本次需求，返回 JSON {allowed:boolean,reasons:[string]}。
拒绝遗漏本次能力或约束、新增事实，以及把贡献模板、历史修复汇报或未来设想当作本次任务。原文未承诺的实现不能变成要求。'''


def validate_scope(record, original):
    if record.get('version') != VERSION:
        raise ValueError('unsupported requirement scope version')
    quotes = record.get('quotes')
    source = original['title'] + '\n' + original['body']
    if (not isinstance(quotes, list) or not quotes
            or any(not isinstance(q, str) or not q.strip() or q not in source for q in quotes)
            or len(quotes) != len(set(quotes))):
        raise ValueError('scope quotes must be distinct exact source passages')
    expected = dict(title='Current requested behavior', body='\n\n'.join(quotes))
    if record.get('document') != expected:
        raise ValueError('scope document differs from selected source passages')
    review = record.get('review', {})
    if review.get('allowed') is not True or not isinstance(review.get('reasons'), list):
        raise ValueError('current requirement scope was not approved')
    return expected


def prepare_document(relay, original, kind):
    if kind != 'pull_request':
        return prepare_issue(relay, original)
    draft = call_json(relay, SYSTEM, original)
    if set(draft) != {'quotes'}:
        raise InvalidDecomposition('invalid requirement selection fields', draft)
    scope = dict(version=VERSION, quotes=draft['quotes'],
                 document=dict(title='Current requested behavior',
                               body='\n\n'.join(draft['quotes']) if isinstance(draft['quotes'], list)
                               and all(isinstance(q, str) for q in draft['quotes']) else ''),
                 review=dict(allowed=True, reasons=[]))
    try:
        validate_scope(scope, original)
    except ValueError as error:
        raise InvalidDecomposition(str(error), draft) from error
    scope['review'] = call_json(relay, AUDIT, dict(original=original, selected=scope['document']))
    try:
        selected = validate_scope(scope, original)
    except ValueError as error:
        raise InvalidDecomposition(str(error), scope) from error
    try:
        return dict(prepare_issue(relay, selected), scope=scope)
    except InvalidDecomposition as error:
        raise InvalidDecomposition(str(error), dict(scope=scope, fragments=error.draft)) from error
