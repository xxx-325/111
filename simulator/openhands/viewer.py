"""Self-contained local report: public transcript and selected private diagnostics."""
import argparse
import json
from pathlib import Path


TEMPLATE = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenHands · 双 Agent 实际运行</title><style>
:root{color-scheme:light dark;--bg:light-dark(#f5f6f8,#16191e);--paper:light-dark(#fff,#20252c);--ink:light-dark(#162334,#e6ebf1);--line:light-dark(#dce2e8,#3a424c);--accent:light-dark(#185b9d,#86bbfa)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:32px 22px}h1{font-size:28px;margin:0}h2{font-size:21px;margin:24px 0 12px}p{margin:8px 0}nav{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}button,select{font:inherit;padding:7px 12px;border:1px solid var(--line);border-radius:8px;color:var(--ink);background:var(--paper)}button[aria-pressed=true]{outline:2px solid var(--accent)}.panel{background:var(--paper);padding:18px;border:1px solid var(--line);border-radius:12px;margin:12px 0}.muted{opacity:.75}.flow{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:15px 0}.node{padding:9px 12px;border:1px solid var(--line);border-radius:7px}.arrow{color:var(--accent)}.row{border-left:3px solid var(--line);padding:8px 16px;margin:15px 0}.row.user{border-color:var(--accent)}.label{font-weight:650;font-size:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace;margin:8px 0}details{margin:10px 0}summary{cursor:pointer}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.metrics{display:flex;gap:22px;flex-wrap:wrap}.metrics strong{display:block;font-size:22px}svg{display:block;width:100%;height:auto}section[hidden]{display:none}.warning{border-left:4px solid #bd8130;padding-left:14px}a{color:var(--accent)}@media(max-width:600px){main{padding:20px 12px}.panel{padding:12px}h1{font-size:24px}}
#graph{overflow-x:auto}#graph svg{min-width:760px}
.comparison{display:grid;grid-template-columns:1fr 1fr;gap:20px}.comparison>div{min-width:0}.review-note{font-size:14px}.translation{font-size:13px;color:var(--accent)}@media(max-width:760px){.comparison{grid-template-columns:1fr}}
</style></head><body><main>
<p class="muted">OPENHANDS SDK 1.47.0 · DEEPSEEK · 实际运行记录</p><h1>两个持续 Agent，如何完成一次交接</h1>
<div class="flow" aria-label="运行流程"><span class="node">当前 issue</span><span class="arrow">→</span><span class="node">User 自主检查／决定</span><span class="arrow">→</span><span class="node">状态机＋发送工具</span><span class="arrow">→</span><span class="node">Code 编码与工具</span><span class="arrow">↺ 最终回复</span></div>
<p>两个独立 Conversation，两个离线工作区。停止 ≠ 完成；接受 ≠ 测试通过。只有专用发送工具中的用户文本进入公开对话。</p>
<label>实际样例 <select id="run"></select></label><div class="panel" id="stats" aria-live="polite"></div>
<nav aria-label="内容"><button data-tab="conversation" aria-pressed="true">纯对话</button><button data-tab="comparison" aria-pressed="false">新旧中文对照</button><button data-tab="dialogue" aria-pressed="false">公开对话＋工具</button><button data-tab="states" aria-pressed="false">状态与压缩</button><button data-tab="review" aria-pressed="false">能力与缺陷复核</button></nav>
<section id="conversation"><p class="muted">仅展示双方实际发出的最终对话，保留原文和顺序；不包含工具调用、状态记录或私有检查。</p><div id="conversation-messages"></div></section>
<section id="comparison" hidden><p class="muted">旧版中文是单独保存的审阅译文，不是原始生成；新版中文为真实生成。按原顺序展示，不强行对齐轮数。复核由助手完成，不是独立真人标注。</p><div id="comparison-messages"></div></section>
<section id="dialogue" hidden><p class="muted">按原顺序展示，不改写 Agent 的回复。工具默认折叠；User 的私有工具不冒充 Code 的工具。</p><div id="messages"></div></section>
<section id="states" hidden><h2>实际发生的状态转移</h2><p>仅画本次运行观察到的边，不加概率。不代表固定流程或已校准的真人分布。</p><div id="graph"></div><div id="transitions"></div><h2>上下文压缩</h2><div id="compression"></div></section>
<section id="review" hidden><div class="panel"><h2>已验证与未验证</h2><p>框架夹具验证了真实文件读写、终端、浏览器导航和点击、发送、自动压缩、同一 Conversation 的进程重启续接。</p><p>真实 issue 样例与框架夹具分开。样例是否完成、测试依据和缺陷以各运行记录为准。语义审核是模型检查，不是形式化保证。</p></div><div id="findings"></div><h2>公开／私有边界</h2><table><tr><th>公开 session</th><th>宿主私有记录</th></tr><tr><td>User 发出的消息、Code 最终回复和真实工具调用／结果</td><td>User 检查、状态申请与拒绝、参考材料、模型请求、SDK 会话、压缩与投递位置</td></tr></table></section>
</main><script id="data" type="application/json">__DATA__</script><script>
const runs=JSON.parse(document.getElementById('data').textContent);const byId=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const select=byId('run');runs.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.name;select.append(o)});
const finalMessages=events=>events.filter(x=>x.kind==='user'||(x.kind==='assistant'&&x.phase==='final'));
const messageHTML=x=>`<article class="row ${x.kind}"><div class="label">#${x.sequence} · ${x.kind==='user'?'User Agent':'Code Agent'}</div><pre>${esc(x.text)}</pre></article>`;
const reviewedMessage=(x,r)=>{const t=r.translations?.by_sequence?.[String(x.sequence)];const note=r.review?.messages?.find(m=>m.sequence===x.sequence);return `<article class="row ${x.kind}"><div class="label">#${x.sequence} · ${x.kind==='user'?'User Agent':'Code Agent'}</div>${t?`<p class="translation">中文审阅译文（非原始生成）</p><pre>${esc(t)}</pre><details><summary>查看英文原文</summary><pre>${esc(x.text)}</pre></details>`:`<pre>${esc(x.text)}</pre>`}${note?`<details class="review-note"><summary>助手复核</summary><p>${esc(note.assessment)}</p></details>`:''}</article>`};
function render(){const r=runs[+select.value],s=r.state,b=r.budget;byId('stats').innerHTML=`<div class="metrics"><span>运行状态<strong>${esc(s.status)}</strong></span><span>已接受任务<strong>${s.accepted.length} / ${r.task_count}</strong></span><span>公开消息<strong>${r.public.filter(x=>['user','assistant'].includes(x.kind)).length}</strong></span><span>Code 工具调用<strong>${r.public.filter(x=>x.kind==='tool_call').length}</strong></span><span>模型成功调用（含压缩／审核）<strong>${b.calls}</strong></span></div><p class="muted">输入 token ${b.prompt_tokens} · 输出 token ${b.completion_tokens} · ${Math.round(r.elapsed_seconds)} 秒${s.pause_reason?' · 暂停原因：'+esc(s.pause_reason):''}</p>`;
byId('stats').innerHTML+=`<p class="muted">原始对话语言：${esc(r.language)} · User：${esc(r.models?.user)} · Code：${esc(r.models?.code)}</p>`;
byId('conversation-messages').innerHTML=finalMessages(r.public).map(messageHTML).join('')||'<p>本次运行还没有公开的最终对话。</p>';
const baseline=runs.find(x=>x.name===r.review?.baseline);byId('comparison-messages').innerHTML=baseline?`<div class="comparison"><div><h2>旧版 · ${esc(baseline.name)}</h2>${finalMessages(baseline.public).map(x=>reviewedMessage(x,baseline)).join('')}</div><div><h2>新版 · ${esc(r.name)}</h2>${finalMessages(r.public).map(x=>reviewedMessage(x,r)).join('')}</div></div>`:finalMessages(r.public).map(x=>reviewedMessage(x,r)).join('');
byId('messages').innerHTML=r.public.map(x=>['user','assistant'].includes(x.kind)?messageHTML(x):`<details class="row"><summary>#${x.sequence} · ${x.kind==='tool_call'?'调用':'结果'} · ${esc(x.tool_name)}</summary><pre>${esc(JSON.stringify(x.action??x.observation,null,2))}</pre></details>`).join('');
const nodes=['RETRIEVE','UNDERSTAND','PLAN','BUILD','OPERATE','DEBUG','EVALUATE'];const names=['查找','理解','规划','实现','执行','调试','评估'];const edges=[...new Set(s.transitions.map(t=>t.previous+'|'+t.state))];
byId('graph').innerHTML=`<svg viewBox="0 0 900 230" role="img" aria-label="实际状态转移图"><defs><marker id="end" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="var(--accent)"/></marker></defs>${edges.map(e=>{const[a,b]=e.split('|').map(v=>nodes.indexOf(v));const x=65+a*128,y=65+b*128;return a===b?`<path d="M${x-18},92 C${x-62},15 ${x+62},15 ${x+18},92" fill="none" stroke="var(--accent)" marker-end="url(#end)"/>`:`<path d="M${x},139 Q${(x+y)/2},220 ${y},139" fill="none" stroke="var(--accent)" marker-end="url(#end)"/>`}).join('')}${nodes.map((n,i)=>`<rect x="${15+i*128}" y="90" width="100" height="50" rx="8" fill="var(--paper)" stroke="var(--line)"/><text x="${65+i*128}" y="120" text-anchor="middle" fill="var(--ink)" font-size="17">${names[i]}</text>`).join('')}</svg>`;
byId('transitions').innerHTML=s.transitions.map(t=>`<details><summary>${esc(t.task_id)} · ${esc(t.previous)} → ${esc(t.state)} · ${esc(t.control)}</summary><p>${esc(t.reason)}</p></details>`).join('')||'<p>没有已接受的转移。</p>';
byId('compression').innerHTML=r.compression.length?r.compression.map(c=>`<p>${esc(c.role)} · ${esc(c.kind)} · 在公开事件 #${c.after_public} 后</p>`).join(''):'<p>本次短样例未触发压缩；自动压缩与恢复已在独立框架夹具验证。</p>';
byId('findings').innerHTML=(r.review?.findings??['尚未完成人工复核；这里不作自然度结论。']).map(x=>`<p class="warning">${esc(x)}</p>`).join('')+(r.review?.metrics?`<details><summary>分项观察（不是自然度总分）</summary><pre>${esc(JSON.stringify(r.review.metrics,null,2))}</pre></details>`:'');}
select.onchange=render;document.querySelectorAll('[data-tab]').forEach(btn=>btn.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(b=>{const on=b===btn;b.setAttribute('aria-pressed',on);byId(b.dataset.tab).hidden=!on})});render();
</script></body></html>'''


def render(directories, output):
    runs = []
    for directory in directories:
        directory = Path(directory)
        checkpoint = json.loads((directory/'private/checkpoint.json').read_text())
        review = directory/'review.json'
        runs.append(dict(name=directory.name, state=checkpoint['state'], public=checkpoint['public'],
                         compression=checkpoint['compression'], budget=checkpoint['budget'],
                         elapsed_seconds=checkpoint['elapsed_seconds'], task_count=len(checkpoint['tasks']),
                         models={role:checkpoint['config'][role]['model'] for role in ('user','code')},
                         language=checkpoint['config'].get('dialogue_language', '旧版未指定（本页旧样例为英文）'),
                         review=json.loads(review.read_text()) if review.exists() else None))
        translations = directory/'translations.zh-CN.json'
        if translations.exists():
            translated = json.loads(translations.read_text())
            public_sequences = {str(x['sequence']) for x in checkpoint['public'] if x['kind'] in ('user','assistant')}
            if translated.get('kind') != 'review_translation' or not set(translated['by_sequence']).issubset(public_sequences):
                raise ValueError('invalid review translation mapping')
            runs[-1]['translations'] = translated
        # Do not embed private check contents, hidden tasks or provider logs in the viewer.
        runs[-1]['state'] = {k: runs[-1]['state'][k] for k in ('status','accepted','transitions','pause_reason') if k in runs[-1]['state']}
    output = Path(output)
    output.write_text(TEMPLATE.replace('__DATA__', json.dumps(runs, ensure_ascii=False).replace('<','\\u003c')))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    render(args.runs, args.output)


if __name__ == '__main__':
    main()
