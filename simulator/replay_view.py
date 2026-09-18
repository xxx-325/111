"""Offline diagnostic viewer. Hidden labels are UI blinding, not access control."""
import argparse
import json
from pathlib import Path


def render(study):
    study = Path(study)
    def read(name):
        return json.loads((study / name).read_text())
    data = dict(cases=read('cases.json'), references=read('references.json'),
                results=read('generation/blind-results.json'), groups=read('generation/groups.json'),
                reviews=read('reviews.json'), summary=read('summary.json'))
    data['tasks'] = read('generation/task-records.json') if (study/'generation/task-records.json').exists() else {}
    data['diagnostics'] = read('generation/results.json') if data['tasks'] else []
    data['task_reviews'] = read('task-reviews.json') if (study/'task-reviews.json').exists() else {}
    payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    return '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>User Agent 回应诊断</title><style>
body{font:16px/1.65 system-ui;background:#f3f5f7;color:#172533;max-width:1050px;margin:auto;padding:24px}
h1{font-size:28px}section,article{background:white;padding:20px;margin:14px 0;border-radius:12px;border:1px solid #dde3ea}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}button,select{padding:10px;font:inherit;margin:5px;border:1px solid #a9bbc9;border-radius:6px;background:white}
.muted{color:#526679;font-size:14px}.badge{font-size:13px;color:#275c8a}summary{cursor:pointer}h3{margin:0}
</style><h1>User Agent：下一步回应合理吗？</h1>
<p id="intro"></p>
<p class="muted">旧样本与新增切点分别统计；来源仍为两段开发 session，没有合适的明确澄清问题点。窗口有省略，不能证明总体真实度。复核为助手分析，非独立真人盲评。隐藏仅用于阅读顺序，数据仍在本文件中。失败保留在分母中。</p>
<details><summary>揭示总体统计与限制（建议先逐条复核）</summary><pre id="stats"></pre></details>
<select id="case"></select><button id="reveal">揭示真人回应与组别</button><div id="body"></div>
<script id="data" type="application/json">''' + payload + '''</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let revealed=false;
document.getElementById('intro').textContent=data.cases.length+' 个切点，'+data.results.length+' 个预定首稿位置。无工具执行、无重生成。先看前文和回应，再揭示参考。';
document.getElementById('stats').textContent='False = 无例子；True = 带例子。长度不是质量分。\\n'+JSON.stringify(data.summary,null,2);
const sel=document.getElementById('case'),body=document.getElementById('body');
for(const c of data.cases){const o=document.createElement('option');o.value=c.id;o.textContent=c.id;sel.append(o)}
function el(tag,text,parent){const n=document.createElement(tag);n.textContent=text;parent.append(n);return n}
function draw(){body.replaceChildren();const c=data.cases.find(x=>x.id===sel.value),ref=data.references.find(x=>x.id===c.id);
 const s=el('section','',body);el('h3','对话前文',s);
 for(const m of c.history){const d=el('details','',s);el('summary',m.role+' · '+m.text.slice(0,85),d);el('pre',m.text,d)}
 s.lastElementChild.open=true;
 for(const r of data.results.filter(x=>x.case_id===c.id)){const a=el('article','',body);const g=data.groups.find(x=>x.id===r.id);
 el('h3',r.id+(revealed?' · '+(g.mode||'baseline')+' · '+(g.examples_enabled?'带例子':'无例子')+' · 第'+g.repeat+'次':''),a);
 el('p','状态：'+r.status,a);
 el('pre',r.message||('生成失败：'+r.error_type),a);
 const d=el('details','',a);el('summary','查看助手复核（建议先自己判断）',d);
 const v=data.reviews.find(x=>x.id===r.id);el('pre',v?v.label+'：'+v.reason:'待复核',d);el('p','私下动作：'+(r.action||'无'),d);
 if(revealed){const diag=data.diagnostics.find(x=>x.id===r.id);if(diag){const d=el('details','',a);el('summary','输入、首稿与状态转移检查',d);el('pre',JSON.stringify(diag,null,2),d)}}
 }
 if(revealed){const a=el('section','',body);el('h3','前文 → 任务记录 → 动作条件 → 回复',a);
 const t=data.tasks[c.id];if(t){el('p','任务整理：'+t.status+'（ready 仅表示结构通过）',a);
 if(data.task_reviews[c.id])el('pre','任务记录复核：'+JSON.stringify(data.task_reviews[c.id],null,2),a);
 if(t.task){el('pre',JSON.stringify(t.task,null,2),a);el('pre',JSON.stringify(t.conditions,null,2),a)}
 const d=el('details','',a);el('summary','任务整理准确输入与首次原始输出',d);el('pre',JSON.stringify(t,null,2),d)}
 el('h3','真人下一条回应（不是唯一正确答案）',a);el('pre',ref.human_reply,a);el('p',ref.category+' · 来源 '+ref.source+' · 截止行 '+ref.cutoff,a)}
}
sel.onchange=()=>{revealed=false;draw()};document.getElementById('reveal').onclick=()=>{revealed=!revealed;draw()};draw();
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    Path(args.output).write_text(render(args.study))


if __name__ == '__main__':
    main()
