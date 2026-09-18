"""Transparent counts, not a combined naturalness score."""
import argparse
from collections import Counter
import json
from pathlib import Path
from .episode import save


def summarize(study):
    study = Path(study)
    def read(name): return json.loads((study / name).read_text())
    results = read('generation/results.json')
    groups = {r['id']: r for r in read('generation/groups.json')}
    reviews = {r['id']: r for r in read('reviews.json')}
    references = {r['id']: r for r in read('references.json')}
    if set(reviews) != {r['id'] for r in results}:
        raise ValueError('every first draft requires a review')
    summary = dict(reviewer='assistant; not independent human blind review', groups={}, scenarios={}, copies=[])
    keys = sorted({(groups[r['id']].get('mode','baseline'), groups[r['id']]['examples_enabled'], references[r['case_id']].get('cohort','existing')) for r in results})
    for mode, enabled, cohort in keys:
        rows = [r for r in results if groups[r['id']]['examples_enabled'] == enabled and groups[r['id']].get('mode','baseline') == mode and references[r['case_id']].get('cohort','existing') == cohort]
        pairs = [[r for r in rows if r['case_id'] == case] for case in references]
        pairs = [p for p in pairs if len(p) == 2 and all(r['status'] == 'generated' for r in p)]
        summary['groups'][f'{cohort}/{mode}/{enabled}'] = dict(count=len(rows),
            statuses=dict(Counter(r['status'] for r in rows)),
            usable_first_drafts=sum(r['status']=='generated' and reviews[r['id']]['label']=='合理' for r in rows),
            issues=dict(Counter(tag for r in rows for tag in reviews[r['id']].get('issues',[]))),
            judgments=dict(Counter(reviews[r['id']]['label'] for r in rows)),
            same_action_pairs=sum(p[0]['action'] == p[1]['action'] for p in pairs),
            same_judgment_pairs=sum(reviews[p[0]['id']]['label'] == reviews[p[1]['id']]['label'] for p in pairs),
            comparable_pairs=len(pairs), length_samples=sum('message' in r for r in rows),
            mean_characters=sum(len(r.get('message', '')) for r in rows)/max(1,sum('message' in r for r in rows)))
    for scenario in ('澄清问题', '完成汇报', '问题未解决', '环境受阻', '方案选择'):
        rows = [r for r in results if references[r['case_id']]['category'] == scenario]
        summary['scenarios'][scenario] = dict(cases=len({r['case_id'] for r in rows}),
            judgments=dict(Counter(reviews[r['id']]['label'] for r in rows)))
    for r in results:
        if 'input' not in r: continue
        packet = json.loads(r['input'][1]['content'])
        for example in packet['examples']:
            if example['reply'].strip() and example['reply'].strip() in r.get('message', ''):
                summary['copies'].append(dict(response=r['id'], example=example['id'],
                    match='entire example reply appears verbatim; does not prove causal copying'))
    task_file = study/'generation/task-records.json'
    if task_file.exists():
        tasks = json.loads(task_file.read_text())
        summary['task_preparation'] = dict(Counter(r['status'] for r in tasks.values()))
        summary['empty_ready_records'] = sum(r['status']=='ready' and not r['task']['current_goal'] and not any(r['task'][k] for k in ('reported_done','open_issues','blockers','questions')) for r in tasks.values())
        summary['task_ready_means'] = 'structurally valid, not semantically correct or informative'
    summary['planned_slots'] = len(results)
    summary['response_calls_started'] = sum('input' in r for r in results)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    args = parser.parse_args()
    save(Path(args.study)/'summary.json', summarize(args.study))


if __name__ == '__main__': main()
