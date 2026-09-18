"""Build an offline comparison from public sessions and explicit human review notes."""
import argparse
import html
import json
import statistics
from pathlib import Path


def read_run(path):
    path = Path(path)
    events = [json.loads(line) for line in (path / 'session.jsonl').read_text().splitlines()]
    checkpoint = json.loads((path / 'private/checkpoint.json').read_text())
    users = [e for e in events if e['kind'] == 'user']
    return dict(name=path.name, events=events, status=checkpoint['status'],
                lengths=[len(e['text']) for e in users],
                tools=sum(e['kind'] in ('tool_result', 'native_tool') for e in events),
                private_checks=sum(e['kind'] in ('tool_result', 'native_tool') for e in checkpoint['audits']))


def render_run(run, reviews):
    esc = html.escape
    parts = [f'<article><h3>{esc(run["name"])}</h3><p>状态：{esc(run["status"])} · 用户消息 {len(run["lengths"])} 条 · '
             f'公开工具结果 {run["tools"]} 次 · 私有检查 {run["private_checks"]} 次</p>',
             '<p>用户消息长度：' + ', '.join(map(str, run['lengths'])) + ' 字符；中位数 ' +
             str(statistics.median(run['lengths']) if run['lengths'] else 0) + '。长度不是自然度分数。</p>']
    calls = {e.get('call_id'): e for e in run['events'] if e['kind'] == 'tool_call'}
    for event in run['events']:
        seq = event['sequence']
        if event['kind'] == 'user':
            note = reviews.get(run['name'], {}).get(str(seq), '尚未人工复核')
            parts.append(f'<section class="user"><div class="label">用户 · #{seq}</div><pre>{esc(event["text"])}</pre><p class="review">复核：{esc(note)}</p></section>')
        elif event['kind'] == 'assistant':
            label = '最终回复' if event.get('phase') == 'final' else '过程说明'
            parts.append(f'<details><summary>Code Agent {label} · #{seq}</summary><pre>{esc(event.get("text", ""))}</pre></details>')
        elif event['kind'] == 'tool_result':
            call = calls.get(event.get('call_id'), {})
            raw = call.get('function', {}).get('arguments', '{}')
            try:
                command = json.loads(raw).get('command', raw)
            except (ValueError, AttributeError):
                command = raw
            parts.append(f'<details class="tool"><summary>工具调用 #{call.get("sequence", "?")} → 返回 #{seq} · exit {event.get("exit_code")}</summary><pre>{esc(command)}</pre><h4>stdout</h4><pre>{esc(event.get("stdout", ""))}</pre><h4>stderr</h4><pre>{esc(event.get("stderr", ""))}</pre></details>')
        elif event['kind'] == 'native_tool':
            parts.append('<details><summary>原生工具事件</summary><pre>' + esc(json.dumps(event, ensure_ascii=False, indent=2)) + '</pre></details>')
    return ''.join(parts) + '</article>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair', nargs=3, action='append', required=True, metavar=('LABEL', 'OLD', 'NEW'))
    parser.add_argument('--review', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    review = json.loads(Path(args.review).read_text())
    sections = []
    for index, (label, old, new) in enumerate(args.pair):
        sections.append(f'<section class="pair" id="pair-{index}" {"hidden" if index else ""}><h2>{html.escape(label)}</h2><div class="columns">' +
                        render_run(read_run(old), review['messages']) + render_run(read_run(new), review['messages']) + '</div></section>')
    buttons = ''.join(f'<button type="button" data-pair="{i}" aria-pressed="{str(i == 0).lower()}">{html.escape(p[0])}</button>' for i, p in enumerate(args.pair))
    text = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>用户表达 · 真实运行对照</title>
<style>body{margin:0;background:#f5f6f8;color:#17243a;font:16px/1.65 system-ui}main{max-width:1350px;margin:auto;padding:28px}h1{font-size:28px}h3{overflow-wrap:anywhere}.columns{display:grid;grid-template-columns:1fr 1fr;gap:22px}article{min-width:0;padding:20px;background:white;border:1px solid #dce2ea;border-radius:12px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace}details{padding:10px 0;border-bottom:1px solid #e2e6ed}summary{cursor:pointer}.user{background:#edf4ff;padding:14px;margin:18px 0;border-left:3px solid #3570cc}.review{font-size:14px;color:#536078}.label{font-weight:600}.note{padding:14px;background:#fff5e2}button{font:inherit;padding:9px 16px;margin:6px;border-radius:8px;border:1px solid #b9c8de;background:white;cursor:pointer}button[aria-pressed=true]{background:#dfeaff}.tool summary{color:#415d78}@media(max-width:760px){main{padding:16px}.columns{grid-template-columns:1fr}article{padding:14px}}</style>
<main><p><a href="../flow.html">← 生成流程</a></p><h1>同样的 issue，用户怎么说话？</h1>
<p>两侧均为实际模型运行产生的模拟用户对话，不是真人聊天。工具命令及返回原样展开；私有验收仅展示次数，不混入公开工具事件。</p>
<p class="note">''' + html.escape(review['summary']) + '</p>' + buttons + ''.join(sections) + '''</main><script>
document.querySelectorAll('[data-pair]').forEach(button=>button.addEventListener('click',()=>{
document.querySelectorAll('.pair').forEach((section,index)=>section.hidden=String(index)!==button.dataset.pair);
document.querySelectorAll('[data-pair]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
}));</script></html>'''
    Path(args.output).write_text(text)
    print(args.output)


if __name__ == '__main__':
    main()
