import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.openhands.commit_scenario import (
    SCHEMA, add_fact_fragments, apply_repository_edits, expand_tasks, load_scenario,
)
from simulator.openhands.disclosure import initial_release, release_after
from simulator.openhands.issue_stages import validate
from simulator.openhands.progressive import progressive_config
from simulator.openhands.prepare_scenario import prepare_scenario


def fact(identifier='f1', **kwargs):
    return dict(id=identifier, type='M1', text='Use the old protocol',
                scope='Customer A', trigger='Code asks about customer deployment', **kwargs)


class CommitScenarioTests(unittest.TestCase):
    def tasks(self):
        return [dict(kind='commit', reference=ref, base=base, title=ref,
                     body='Infer original', patch='', identifier=ref)
                for ref, base in [('a', 'base'), ('b', 'a')]]

    def scenario(self):
        return dict(schema=SCHEMA, commits=[
            dict(commit='a', facts=[fact()], extensions=[
                dict(title='Batch', body='Add a batch entry', facts=[fact('f2', supersedes=['f1'])]),
                dict(title='Export', body='Add an export entry')]),
            dict(commit='b', extensions=[]),
        ])

    def test_each_original_is_retained_and_extensions_inherit_history(self):
        tasks = expand_tasks(self.tasks(), (self.scenario(), 'hash'))
        self.assertEqual([t['kind'] for t in tasks],
                         ['commit', 'scenario_extension', 'scenario_extension', 'commit'])
        self.assertEqual([t['reference'] for t in tasks if t['kind'] == 'commit'], ['a', 'b'])
        self.assertIsNone(tasks[1]['reference'])
        self.assertEqual(tasks[1]['scenario']['source_commit'], 'a')
        self.assertEqual([f['id'] for f in tasks[-1]['scenario']['inherited_facts']], ['f1', 'f2'])
        self.assertEqual(tasks[1]['scenario']['facts'][0]['supersedes'], ['f1'])
        self.assertEqual([t['scenario']['source_task_index'] for t in tasks], [0, 0, 0, 1])

    def test_skip_reorder_or_forward_correction_is_rejected(self):
        scenario = self.scenario()
        scenario['commits'].reverse()
        with self.assertRaisesRegex(ValueError, 'ordered'):
            expand_tasks(self.tasks(), (scenario, 'hash'))
        scenario = self.scenario()
        scenario['commits'][0]['facts'][0]['supersedes'] = ['future']
        with self.assertRaisesRegex(ValueError, 'earlier'):
            expand_tasks(self.tasks(), (scenario, 'hash'))

    def test_facts_are_not_released_by_elapsed_rounds(self):
        source = dict(title='Original', body='Add streaming support')
        plan = validate({'items': [dict(id='s1', category='symptom', text='Add streaming support',
                                       source_quote=source['body'], requires=[], related=[])]}, source)
        scenario = expand_tasks(self.tasks(), (self.scenario(), 'hash'))[1]['scenario']
        plan, _, triggers = add_fact_fragments(plan, source, scenario)
        released = initial_release(plan)
        self.assertEqual(released, ['s1'])
        self.assertEqual(release_after(plan, released, {'outcome': 'unsolved'},
                                       triggered_only=triggers), ['s1'])
        self.assertEqual(release_after(plan, released, {'outcome': 'uncertain',
                             'requested_fragment_ids': ['external2']},
                             triggered_only=triggers), ['s1', 'external2'])
        self.assertIn('updates the earlier condition', plan['items'][-1]['text'])

    def test_scenario_digest_is_pinned_for_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scenario.json'
            path.write_text(json.dumps(self.scenario()))
            config = dict(scenario_file=str(path), continuous_commits=True, progressive_issues=True)
            before = progressive_config(config)['_scenario_sha256']
            path.write_text(json.dumps(self.scenario(), indent=2))
            self.assertNotEqual(before, progressive_config(config)['_scenario_sha256'])
            with self.assertRaisesRegex(ValueError, 'continuous'):
                load_scenario(dict(config, continuous_commits=False))
            with self.assertRaisesRegex(ValueError, 'continuous'):
                load_scenario(dict(config, swe_chain_evo={}))

    def test_new_task_remembers_user_known_facts_without_releasing_new_correction(self):
        from simulator.openhands.progressive import ProgressiveEpisode
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode = object.__new__(ProgressiveEpisode)
            tasks = expand_tasks(self.tasks(), (self.scenario(), 'hash'))
            document = {key: tasks[1][key] for key in ('title', 'body')}
            plan = validate({'items': [dict(id='s1', category='symptom', text=document['body'],
                source_quote=document['body'], requires=[], related=[])]}, document)
            prepared = root / 'prepared.json'
            prepared.write_text(json.dumps({'cases': [{}, dict(issue=document, plan=plan,
                                                              review={'allowed': True})]}))
            episode.private = root
            episode.config = dict(prepared_issues=str(prepared))
            episode.saved = dict(tasks=tasks)
            episode.progress = dict(tasks={'task-1': dict(released=['external1'],
                fact_triggers={'external1': {'fact_id': 'f1'}})})
            episode.state = SimpleNamespace(data={'task_id': 'task-2', 'task_index': 1})
            episode.persist = Mock()
            current = episode.current()
            self.assertEqual(current['released'], ['s1', 'external1'])
            self.assertNotIn('external2', current['released'])
            self.assertIn('Customer A', episode.requirement()['body'])
            self.assertEqual(current['fact_triggers']['external2']['supersedes'], ['f1'])

    def test_repository_edit_checks_all_preconditions_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a').write_text('old')
            edits = [dict(path='a', before='old', after='misleading'),
                     dict(path='b', before='wrong', after='changed')]
            with self.assertRaisesRegex(ValueError, 'precondition'):
                apply_repository_edits(root, edits)
            self.assertEqual((root / 'a').read_text(), 'old')
            edits[1]['before'] = None
            apply_repository_edits(root, edits)
            self.assertEqual((root / 'a').read_text(), 'misleading')
            self.assertEqual((root / 'b').read_text(), 'changed')

    def test_repository_edit_cannot_target_git_or_external_paths(self):
        for path in ('../secret', '/secret', '.git/config'):
            scenario = self.scenario()
            scenario['commits'][0]['repository_edits'] = [dict(
                path=path, before=None, after='x', type='M3', reason='controlled decoy')]
            with self.assertRaisesRegex(ValueError, 'safe path'):
                expand_tasks(self.tasks(), (scenario, 'hash'))

    def test_episode_applies_perturbation_once_and_pauses_uncertain_replay(self):
        from simulator.openhands.progressive import ProgressiveEpisode
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            episode = object.__new__(ProgressiveEpisode)
            episode.root = Path(directory)
            candidate = episode.root / 'workspace/candidate'
            candidate.mkdir(parents=True)
            (candidate / 'a').write_text('original')
            episode.saved = {'tasks': [{'scenario': {'repository_edits': [dict(
                path='a', before='original', after='decoy')]}}]}
            episode.state = SimpleNamespace(data={'task_index': 0})
            current = {}
            episode.current = lambda: current
            episode.agents = {'code': Mock()}
            episode.persist = Mock()
            episode.before_code_turn()
            self.assertEqual((candidate / 'a').read_text(), 'decoy')
            (candidate / 'a').write_text('agent repair')
            episode.apply_scenario_edits()
            self.assertEqual((candidate / 'a').read_text(), 'agent repair')
            current.pop('perturbation_applied')
            with self.assertRaisesRegex(RuntimeError, 'Uncertain'):
                episode.apply_scenario_edits()

    def test_expanded_task_commands_use_original_commit_index(self):
        from simulator.openhands.progressive import ProgressiveEpisode
        from types import SimpleNamespace
        episode = object.__new__(ProgressiveEpisode)
        episode.saved = {'tasks': expand_tasks(self.tasks(), (self.scenario(), 'hash'))}
        episode.config = {'tasks': [dict(user_run_commands=['first']), dict(user_run_commands=['second'])]}
        episode.current = lambda: dict(released=['s1'], plan={'items': [dict(id='s1'), dict(id='external1')]},
                                       fact_triggers={'external1': {}})
        episode.state = SimpleNamespace(data={'task_index': 3})
        self.assertEqual(episode.user_run_commands(), ['second'])
        episode.state.data['task_index'] = 1
        self.assertEqual(episode.user_run_commands(), [])

    def test_real_contiguous_chain_prepares_all_originals_and_multiple_extensions(self):
        from simulator.tasks import prepare, snapshot
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            refs = []
            for value in ('base', 'first', 'second'):
                (repo / 'entry').write_text(value)
                subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                                'user.email=fixture@example.invalid', 'commit', '-qm', value], check=True)
                refs.append(subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip())
            config = dict(repository=str(repo), base=refs[0], continuous_commits=True,
                          progressive_issues=True, tasks=[dict(commit=ref) for ref in refs[1:]])
            _, _, originals = prepare(config, root, include_patch=False)
            settings = self.scenario()
            drafts = [{k: row.get(k, []) for k in ('facts', 'repository_edits', 'extensions')}
                      for row in settings['commits']]
            with patch('simulator.openhands.prepare_scenario.call_json',
                       side_effect=[drafts[0], {'allowed': True}, drafts[1], {'allowed': True}]) as calls:
                prepare_scenario(None, originals, repo, root / 'frozen')
            self.assertEqual(calls.call_count, 4)
            self.assertEqual(calls.call_args_list[0].args[2]['source']['base_files'], {'entry': 'base'})
            self.assertIn('-base\n', calls.call_args_list[0].args[2]['source']['patch'])
            self.assertIn('+first\n', calls.call_args_list[0].args[2]['source']['patch'])
            _, base, expanded = prepare(dict(config, scenario_file=str(root / 'frozen/scenario.json')),
                                        root, include_patch=False)
            self.assertEqual([task['reference'] for task in expanded], [refs[1], None, None, refs[2]])
            self.assertTrue(all(task['patch'] == '' for task in expanded))
            snapshot(repo, base, root / 'candidate')
            self.assertEqual((root / 'candidate/entry').read_text(), 'base')
            self.assertFalse((root / 'candidate/.git').exists())
            self.assertEqual(json.loads((root / 'frozen/report.json').read_text())['status'], 'candidate_pass')

    def test_rejected_scenario_keeps_draft_but_does_not_freeze_or_resample(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'frozen'
            draft = dict(facts=[], repository_edits=[], extensions=[])
            with patch('simulator.openhands.prepare_scenario.source_context', return_value={'patch': 'diff'}), \
                    patch('simulator.openhands.prepare_scenario.call_json',
                          side_effect=[draft, {'allowed': False, 'reasons': ['unrelated']}]) as calls:
                with self.assertRaisesRegex(ValueError, 'rejected'):
                    prepare_scenario(None, self.tasks(), None, output)
            self.assertEqual(calls.call_count, 2)
            self.assertTrue((output / 'commit-1-draft.json').exists())
            self.assertFalse((output / 'scenario.json').exists())
