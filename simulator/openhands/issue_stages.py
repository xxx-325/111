"""Private, source-grounded progressive issue preparation."""
import base64
import json
import uuid
import re
import copy
from ..api_agent import parse_object

CATEGORIES = ('symptom', 'cause')
SCHEMA = 'issue-fragments-v5-minimal-output'
AUDIT_VERSION = 'fragment-audit-v6-grounded-opening'
AUDIT_SYSTEM = '''检查片段是否忠实覆盖需求资料，返回 JSON {allowed:boolean,reasons:[string]}。
开场风格、术语多少或轻微分类偏差仅在 reasons 中提示，不据此拒绝。拒绝新增事实、遗漏本次重要需求、直接泄露参考实现、损坏完整复现或把未确认原因写成事实；引用中已有的技术背景不等于泄漏答案。'''
SYSTEM = '''把需求资料拆成可逐次提供的信息片段，不写对话。
返回 JSON {items:[{category,text,source_quote}]}；symptom 表示用户诉求、现象、场景或约束，cause 表示原文已有的原因线索。
首项 text 用普通使用者语言概括所需能力或用户可见问题，不带项目/类/函数名、参数或原因；再给具体场景、约束或完整复现，最后给原因线索。每项是一个有意义的信息单元，
issue 中每个代码或报错块必须整体成为一个 symptom 的 source_quote，不能省略或拆开。
不按字段或代码行强拆，不补造信息。text 用中文；source_quote 必须是支持它的原文原句。'''


class InvalidDecomposition(ValueError):
    """Retain the unedited model draft when structural validation fails."""
    def __init__(self, message, draft):
        super().__init__(message)
        self.draft = draft


def call_json(relay, system, data):
    body=dict(model=relay.config['model'],stream=False,temperature=0,max_tokens=relay.config.get('max_output_tokens'),
        response_format={'type':'json_object'},messages=[{'role':'system','content':system},
            {'role':'user','content':json.dumps(data,ensure_ascii=False)}])
    status,raw=relay.dispatch(dict(id='prepare-'+uuid.uuid4().hex,path='/v1/chat/completions',
        body=base64.b64encode(json.dumps(body).encode()).decode()))
    if status != 200:
        raise RuntimeError('Preparation provider call failed')
    return parse_object(json.loads(raw)['choices'][0]['message']['content'])


def enrich_draft(draft):
    """Attach deterministic IDs and relations that the model does not need to manage."""
    if set(draft) != {'items'} or not isinstance(draft['items'], list) or not draft['items']:
        raise ValueError('Expected disclosure items only')
    counts = {category: 0 for category in CATEGORIES}
    items = []
    for row in draft['items']:
        if set(row) != {'category', 'text', 'source_quote'} or row.get('category') not in CATEGORIES:
            raise ValueError('Invalid minimal disclosure fields')
        category = row['category']
        counts[category] += 1
        items.append(dict(row, id=category[0] + str(counts[category]), requires=[], related=[]))
    symptoms = [row['id'] for row in items if row['category'] == 'symptom']
    for row in items:
        if row['category'] == 'cause':
            row['related'] = list(symptoms)
    return {'items': items}


def validate(plan, issue, *, normalizations=None):
    source=issue['title']+'\n'+issue['body']
    if set(plan) != {'items'} or not isinstance(plan['items'], list) or not plan['items']:
        raise ValueError('Expected disclosure items only')
    for row in plan['items']:
        if set(row) != {'id','category', 'text', 'source_quote','requires','related'} or row['category'] not in CATEGORIES:
            raise ValueError('Invalid disclosure category or fields')
        if not isinstance(row['id'],str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}',row['id']):
            raise ValueError('Invalid fragment identifier')
        if not isinstance(row['text'], str) or not row['text'].strip():
            raise ValueError('Empty disclosure item')
        if not isinstance(row.get('source_quote'),str) or not row['source_quote'] or row['source_quote'] not in source:
            raise ValueError('Ungrounded issue quotation')
    items=[copy.deepcopy(row) for category in CATEGORIES for row in plan['items'] if row['category']==category]
    by_id={row['id']:row for row in items}
    if len(by_id)!=len(items) or len({' '.join(row['text'].split()) for row in items})!=len(items):
        raise ValueError('Duplicate fragment ID or content')
    if not any(row['category']=='symptom' for row in items):
        raise ValueError('At least one symptom is required')
    quoted = '\n'.join(row['source_quote'] for row in items)
    for block in re.findall(r'```[\s\S]*?```', source):
        if block not in quoted:
            raise ValueError('Issue code or error block was omitted or split')
    for row in items:
        for field in ('requires','related'):
            refs=row[field]
            if not isinstance(refs,list) or any(not isinstance(r,str) or r not in by_id for r in refs) or len(refs)!=len(set(refs)):
                raise ValueError('Invalid fragment relation')
    # Associations express the same link in either direction. Canonicalize only
    # cross-category associations; never repair content or dependency semantics.
    for row in items:
        if row['category'] != 'symptom':
            continue
        for identifier in row['related']:
            cause=by_id[identifier]
            if cause['category'] != 'cause':
                raise ValueError('Associations must connect a symptom and a cause')
            duplicate=row['id'] in cause['related']
            if not duplicate:
                cause['related'].append(row['id'])
            if normalizations is not None:
                normalizations.append(dict(operation='reverse_association',
                    original_from=row['id'],original_to=identifier,
                    canonical_from=identifier,canonical_to=row['id'],deduplicated=duplicate))
        row['related']=[]
    prior=set()
    for row in items:
        if any(r not in prior for r in row['requires']):
            raise ValueError('Prerequisites must precede the fragment; cycles are forbidden')
        if row['category']=='cause' and (not row['related'] or any(by_id[r]['category']!='symptom' for r in row['related'])):
            raise ValueError('Each cause must link to existing symptoms')
        prior.add(row['id'])
    return dict(schema=SCHEMA, items=items)


def prepare_issue(relay, issue):
    draft=call_json(relay,SYSTEM,issue)
    normalizations=[]
    try:
        plan=validate(enrich_draft(draft),issue,normalizations=normalizations)
    except ValueError as error:
        raise InvalidDecomposition(str(error),draft) from error
    return dict(plan=plan,raw_draft=draft,normalizations=normalizations,review=audit_issue(relay,issue,plan))


def audit_issue(relay, issue, plan):
    """Review a frozen extraction without another decomposition or any rewriting."""
    if plan.get('schema') != SCHEMA or validate({'items':plan.get('items')},issue) != plan:
        raise ValueError('Cannot audit an invalid or old fragment plan')
    review=call_json(relay,AUDIT_SYSTEM,dict(issue=issue,plan=plan))
    if type(review.get('allowed')) is not bool or not isinstance(review.get('reasons'),list) or any(not isinstance(r,str) for r in review['reasons']):
        raise ValueError('Invalid decomposition audit result')
    if not review['allowed'] and not any(reason.strip() for reason in review['reasons']):
        raise ValueError('Rejected audit must provide a reason')
    return dict(review,audit_version=AUDIT_VERSION)


def visible_requirement(plan, released):
    from .disclosure import check_released
    from .fragment_text import fragment_text
    check_released(plan,released)
    return {'body':'\n\n'.join(fragment_text(s) for s in plan['items'] if s['id'] in released)}
