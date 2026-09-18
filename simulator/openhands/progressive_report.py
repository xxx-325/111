"""Local Chinese review of released requirements and private Judge decisions."""
import html
import json
from pathlib import Path


def render_preparation(root, report):
    """Render original extraction with a labeled, local-only disclosure preview."""
    root=Path(root)
    review_path=root/'assistant-review.json'
    review=json.loads(review_path.read_text()) if review_path.exists() else {'status':'尚未完成助手复核'}
    data=json.dumps(dict(report=report,review=review),ensure_ascii=False).replace('<','\\u003c')
    page=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Issue 信息片段审阅</title><style>
body{max-width:1120px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui;background:#f5f7fb;color:#243047}
section,article,aside{background:white;padding:18px;border:1px solid #d8e0eb;border-radius:12px;margin:14px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 monospace}
button{padding:8px 15px;margin:5px;border:1px solid #aab7ca;border-radius:7px;background:#eef3fa;cursor:pointer}
.cause{border-left:5px solid #c98627}.symptom{border-left:5px solid #487ec0}.muted{color:#637187}summary{cursor:pointer}
@media(max-width:760px){.grid{grid-template-columns:1fr}}</style>
<h1>原始 Issue → 信息片段 → 释放预览</h1>
<p>左侧是私有完整拆解，右侧仅预览已选择内容。按钮是程序规则演示，不代表真实 Agent 已收到信息。</p>
<section id="status"></section><main id="cases"></main>
<details><summary>全部原始记录与资源统计</summary><pre id="raw"></pre></details>
<p><a href="report.json">原始拆解与自动审核</a> · <a href="provider.jsonl">模型输入输出（私有）</a></p>
<script id="data" type="application/json">DATA_PLACEHOLDER</script><script>
const data=JSON.parse(document.getElementById('data').textContent);
document.getElementById('status').textContent='处理状态：'+data.report.status+'；复核记录：'+JSON.stringify(data.review);
document.getElementById('raw').textContent=JSON.stringify(data,null,2);
function el(tag,text,where){const n=document.createElement(tag);if(text!==null)n.textContent=text;if(where)where.append(n);return n;}
if(data.report.raw_draft){
 const failed=el('section',null,document.getElementById('cases'));
 el('h2','未通过结构校验的原始草稿（未改写，不提供释放预览）',failed);
 el('pre',JSON.stringify(data.report.raw_draft,null,2),failed);
}
for(const c of data.report.cases||[]){
 const section=el('section',null,document.getElementById('cases'));el('h2',c.id,section);
 if(c.normalizations?.length){
  const conversions=el('details',null,section);el('summary','仅关联格式转换；片段内容未改写',conversions);
  el('pre',JSON.stringify(c.normalizations,null,2),conversions);
 }
 if(c.raw_draft){
  const draft=el('details',null,section);el('summary','模型原始草稿（转换前）',draft);
  el('pre',JSON.stringify(c.raw_draft,null,2),draft);
 }
 const original=el('details',null,section);el('summary','原始 issue（完整资料，仅供审阅）',original);el('pre',c.issue.title+'\n\n'+c.issue.body,original);
 if(c.scope||c.projection){const projected=el('details',null,section);el('summary',c.projection?'由提交反推的需求（非真人原文）':'从 PR 选取的当前需求',projected);el('pre',JSON.stringify(c.scope||c.projection,null,2),projected);}
 const grid=el('div',null,section);grid.className='grid';const left=el('div',null,grid);const right=el('aside',null,grid);
 const items=c.plan?.items||[];let released=new Set();
 const output=el('pre','',right);const positions=el('p','',right);
 function show(){output.textContent=items.filter(i=>released.has(i.id)).map(i=>i.text).join('\n\n');positions.textContent='预览已释放：'+[...released].join(', ');}
 function add(id){const i=items.find(i=>i.id===id);if(!i||released.has(id))return;const d=i.requires.find(d=>!released.has(d));if(d){add(d);return;}released.add(id);}
 function initial(){released=new Set();const first=items.find(i=>i.category==='symptom');if(first)add(first.id);show();}
 el('button','重置到第一份现象',right).onclick=initial;
 el('button','模拟未解决：补下一条',right).onclick=()=>{const i=items.find(i=>!released.has(i.id));if(i)add(i.id);show();};
 for(const i of items){const card=el('article',null,left);card.className=i.category;
  el('b',(i.category==='cause'?'可能原因':'现象')+' · '+i.id,card);el('pre',i.text,card);
  const source=el('details',null,card);el('summary','原文依据与关联',source);el('pre',i.source_quote,source);
  el('p','前置：'+i.requires.join(', ')+'；关联现象：'+i.related.join(', '),source);
  el('button','仅匹配此片段＋必要前置',card).onclick=()=>{add(i.id);show();};
 }initial();
 if(!items.length)el('p','未取得有效拆解；请查看原始记录，未生成预览。',left);
}
</script></html>'''
    (root/'index.html').write_text(page.replace('DATA_PLACEHOLDER',data))


def render(root, saved):
    def pretty(value):
        return '<pre>' + html.escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'
    def details(title, value):
        return '<details><summary>' + html.escape(title) + '</summary>' + pretty(value) + '</details>'
    public = saved.get('public', [])
    body = '<h1>渐进需求与三方检查</h1><p>公开 session 仅含 User、Code 回复和 Code 工具事件。Judge 记录是私有诊断，不是 User 亲自测试的证据。</p>'
    body += '<p>运行状态：' + html.escape(saved['state']['status']) + ' · ' + html.escape(saved['state'].get('pause_reason', '')) + '</p>'
    body += '<nav>逐步释放需求 → User 委托 → Code 工作 → Judge 检查 → User 回应或结束</nav>'
    for message in public:
        if message['kind'] in ('user', 'assistant'):
            body += '<article><b>' + ('User' if message['kind']=='user' else 'Code') + '</b><pre>' + html.escape(message['text']) + '</pre></article>'
        else:
            body += details('Code 工具：' + str(message.get('tool_name', '')), message)
        for record in saved.get('progressive', {}).get('decisions', {}).values():
            if record['job']['code_reply']['id'] == message['id']:
                body += '<section><h2>Judge：' + html.escape(record['payload'].get('outcome', '无结论')) + '</h2>'
                body += pretty(dict(审核通过=record['accepted'], 公开反馈=record['payload'].get('public_feedback', {})))
                body += details('本轮释放变化与 User 可见需求', record.get('disclosure', {}))
                body += details('私有检查、真实工具与审核（不发给 User／Code）', record) + '</section>'
    body += details('逐任务信息片段、来源与释放历史（私有诊断）', saved.get('progressive', {}).get('tasks', {}))
    body += details('状态转移', saved['state']['transitions'])
    body += details('压缩位置', saved.get('compression', []))
    body += details('资源统计', saved.get('budget', {}))
    body += '<p>这是助手审阅页，不是独立人工标注。运行完成不代表 Judge 判断可靠或对话达到真人水平。</p>'
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>渐进 Issue 审阅</title><style>body{max-width:1000px;margin:32px auto;padding:0 20px;font:16px/1.65 system-ui;background:#f5f7fb;color:#202c3c}article,section,details,nav{background:white;border:1px solid #d9e0ea;border-radius:10px;padding:16px;margin:14px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.65 ui-monospace,monospace}summary{cursor:pointer}section{border-left:5px solid #577aac}</style>' + body + '</html>'
    (Path(root)/'index.html').write_text(document)
