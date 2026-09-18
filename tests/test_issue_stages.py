import copy
import unittest
from unittest.mock import patch
from simulator.openhands.issue_stages import validate,visible_requirement,prepare_issue,audit_issue,enrich_draft,SCHEMA,AUDIT_VERSION,AUDIT_SYSTEM,InvalidDecomposition
from simulator.openhands.disclosure import (initial_release, release_after, include,
                                            release_feedback)


def fixture():
    issue={'title':'daterange wrong in December because months start at 1','body':'annual step; reproduction code'}
    def item(identifier,category,text,quote,requires=None,related=None):
        return dict(id=identifier,category=category,text=text,source_quote=quote,requires=requires or [],related=related or [])
    raw={'items':[
        item('s1','symptom','daterange 生成日期不对','daterange wrong'),
        item('s2','symptom','十二月起点','in December',['s1']),
        item('s3','symptom','年度步进','annual step',['s1']),
        item('s4','symptom','完整复现代码','reproduction code',['s1']),
        item('c1','cause','可能与月份从 1 开始有关','because months start at 1',['s2'],['s2'])]}
    return issue,raw


class StageTests(unittest.TestCase):
    def setUp(self):
        self.issue,self.raw=fixture()
        self.plan=validate(self.raw,self.issue)

    def test_mixed_title_and_same_group_stay_separate(self):
        self.assertEqual(len(self.plan['items']),5)
        first=initial_release(self.plan)
        self.assertEqual(first,['s1'])
        self.assertNotIn('月份',str(visible_requirement(self.plan,first)))
        self.assertNotIn('source_quote',str(visible_requirement(self.plan,first)))

    def test_targeted_nonprefix_disclosure(self):
        released=release_after(self.plan,['s1'],dict(outcome='uncertain',requested_fragment_ids=['s4']))
        self.assertEqual(released,['s1','s4'])
        text=str(visible_requirement(self.plan,released))
        self.assertNotIn('十二月',text)
        self.assertNotIn('年度',text)
        self.assertEqual(include(self.plan,released,['s4']),released)

    def test_prerequisites_only_not_all_prior_fragments(self):
        first = include(self.plan,['s1'],['c1'])
        self.assertEqual(first,['s1','s2'])
        self.assertEqual(include(self.plan,first,['c1']),['s1','s2','c1'])

    def test_multiple_requests_release_only_one_and_replay_is_stable(self):
        verdict = dict(outcome='uncertain', requested_fragment_ids=['s4','c1'])
        before = ['s1']
        self.assertEqual(release_after(self.plan,before,verdict),['s1','s4'])
        self.assertEqual(release_after(self.plan,before,verdict),['s1','s4'])
        self.assertEqual(before,['s1'])
        self.assertEqual(release_after(self.plan,before,dict(verdict,outcome='solved')),before)

    def test_default_order_end_success_unknown(self):
        released=initial_release(self.plan)
        for expected in (['s1','s2'],['s1','s2','s3'],['s1','s2','s3','s4'],['s1','s2','s3','s4','c1']):
            released=release_after(self.plan,released,dict(outcome='unsolved'))
            self.assertEqual(released,expected)
        self.assertEqual(release_after(self.plan,released,dict(outcome='unsolved')),released)
        for outcome in ('solved','uncertain'):
            self.assertEqual(release_after(self.plan,['s1'],dict(outcome=outcome)),['s1'])

    def test_no_cause_no_padding(self):
        self.raw['items']=self.raw['items'][:1]
        plan=validate(self.raw,self.issue)
        self.assertEqual(len(plan['items']),1)
        self.assertEqual(release_after(plan,['s1'],dict(outcome='unsolved')),['s1'])

    def test_released_reproduction_preserves_original_source_without_mutation(self):
        from simulator.openhands.fragment_text import fragment_text
        quote='for day in daterange(start, end):\r\n    print(repr(day))'
        item=dict(category='symptom',text='遍历日期并打印',source_quote=quote)
        original=copy.deepcopy(item)
        self.assertIn(quote,fragment_text(item))
        self.assertEqual(item,original)
        self.assertEqual(fragment_text(dict(item,category='cause')),item['text'])
        self.assertEqual(fragment_text(dict(item,source_quote='because months start at one')),item['text'])
        self.assertEqual(fragment_text(dict(item,text=quote)),quote)

    def test_invalid_quotes_duplicates_and_relations(self):
        for change in ({'source_quote':'invented'},{'id':'s2'},{'text':'十二月起点'},
                       {'requires':['s3']},{'requires':['future']},{'related':['s2']}):
            raw=copy.deepcopy(self.raw);raw['items'][0].update(change)
            with self.assertRaises(ValueError):validate(raw,self.issue)
        raw=copy.deepcopy(self.raw);raw['items'][-1]['related']=['future']
        with self.assertRaises(ValueError):validate(raw,self.issue)

    def test_no_conditions_or_old_cache(self):
        with self.assertRaises(ValueError):validate(dict(self.raw,criteria=[]),self.issue)
        self.assertEqual(self.plan['schema'],SCHEMA)
        self.assertNotIn('stages',self.plan)
        with self.assertRaises(ValueError):visible_requirement(self.plan,0)

    def test_one_decomposition_one_audit_no_reference_input(self):
        draft={'items':[{k:item[k] for k in ('category','text','source_quote')}
                        for item in self.raw['items']]}
        expected=validate(enrich_draft(draft),self.issue)
        with patch('simulator.openhands.issue_stages.call_json',side_effect=[draft,{'allowed':True,'reasons':[]}]) as call:
            result=prepare_issue(object(),self.issue)
        self.assertEqual(call.call_count,2)
        self.assertEqual(call.call_args_list[0].args[2],self.issue)
        self.assertNotIn('reference',str(call.call_args_list[0].args[2]))
        self.assertEqual(result['plan'],expected)
        self.assertEqual(result['raw_draft'],draft)

    def test_reaudit_one_call_frozen_plan_and_separate_policy(self):
        before=copy.deepcopy(self.plan)
        with patch('simulator.openhands.issue_stages.call_json',return_value={'allowed':True,'reasons':[]}) as call:
            result=audit_issue(object(),self.issue,self.plan)
        self.assertEqual(call.call_count,1)
        self.assertEqual(call.call_args.args[1],AUDIT_SYSTEM)
        self.assertEqual(call.call_args.args[2],dict(issue=self.issue,plan=before))
        self.assertEqual(self.plan,before)
        self.assertEqual(result['audit_version'],AUDIT_VERSION)

    def test_reaudit_preserves_rejection_and_never_retries(self):
        reason='s1 includes a fabricated test result absent from the issue'
        with patch('simulator.openhands.issue_stages.call_json',return_value={'allowed':False,'reasons':[reason]}) as call:
            result=audit_issue(object(),self.issue,self.plan)
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reasons'],[reason])
        self.assertEqual(call.call_count,1)

    def test_invalid_audit_response_fails_closed(self):
        for response in ({'allowed':False,'reasons':[]},{'allowed':'true','reasons':[]},{'allowed':True}):
            with patch('simulator.openhands.issue_stages.call_json',return_value=response) as call:
                with self.assertRaises(ValueError):audit_issue(object(),self.issue,self.plan)
                self.assertEqual(call.call_count,1)

    def test_invalid_cached_plan_never_sent_for_review(self):
        invalid=copy.deepcopy(self.plan);invalid['items'][0]['source_quote']='invented'
        with patch('simulator.openhands.issue_stages.call_json') as call:
            with self.assertRaises(ValueError):audit_issue(object(),self.issue,invalid)
            call.assert_not_called()

    def test_meaningful_unit_fixture_is_not_split_by_fields(self):
        # This verifies host grouping mechanics, not model extraction quality.
        raw=copy.deepcopy(self.raw)
        raw['items']=[raw['items'][0],dict(id='details',category='symptom',
            text='十二月起点、年度步进时异常；完整复现代码如下。',
            source_quote='annual step; reproduction code',requires=['s1'],related=[]),raw['items'][-1]]
        raw['items'][-1].update(requires=['details'],related=['details'])
        plan=validate(raw,self.issue)
        first=initial_release(plan)
        second=release_after(plan,first,dict(outcome='unsolved'))
        self.assertEqual(second,['s1','details'])
        self.assertIn('十二月起点、年度步进',visible_requirement(plan,second)['body'])
        self.assertNotIn('月份从 1',visible_requirement(plan,second)['body'])

    def test_visible_requirement_retains_source_title(self):
        result = visible_requirement(self.plan, initial_release(self.plan))
        self.assertNotIn('title', result)

    def test_old_granularity_cache_rejected_without_provider_call(self):
        old=copy.deepcopy(self.plan);old['schema']='issue-fragments-v3'
        with patch('simulator.openhands.issue_stages.call_json') as call:
            with self.assertRaises(ValueError):audit_issue(object(),self.issue,old)
            call.assert_not_called()

    def test_invalid_draft_retained_without_audit_or_regeneration(self):
        raw={'items':[{'category':'symptom','text':'daterange 生成日期不对',
                       'source_quote':'daterange wrong','extra':'not allowed'}]}
        with patch('simulator.openhands.issue_stages.call_json',return_value=raw) as call:
            with self.assertRaises(InvalidDecomposition) as caught:prepare_issue(object(),self.issue)
        self.assertEqual(caught.exception.draft,raw)
        self.assertEqual(call.call_count,1)

    def test_fenced_reproduction_cannot_be_omitted_or_split(self):
        issue={'title':'round trip fails','body':'Example:\n```python\nprint(run())\n```'}
        omitted={'items':[{'category':'symptom','text':'往返结果不一致',
                            'source_quote':'round trip fails'}]}
        with self.assertRaises(ValueError):
            validate(enrich_draft(omitted),issue)
        complete=copy.deepcopy(omitted)
        complete['items'].append({'category':'symptom','text':'完整复现如下',
            'source_quote':'```python\nprint(run())\n```'})
        self.assertEqual(len(validate(enrich_draft(complete),issue)['items']),2)

    def test_judge_feedback_releases_one_unit_then_progresses_same_failure(self):
        feedback = {'kind': 'wrong_output', 'evidence_id': 'obs1',
                    'input': 'prefix@example.com', 'output': 'original',
                    'symptom': '默认值查找仍未生效'}
        first = release_feedback(feedback)
        self.assertEqual(len(first['added']), 1)
        self.assertEqual(first['released_unit_ids'], [first['units'][0]['id']])
        second = release_feedback(feedback, first['released_unit_ids'],
                                  first['failure_key'])
        self.assertEqual(len(second['added']), 1)
        self.assertEqual(second['released_unit_ids'],
                         [unit['id'] for unit in second['units']])
        third = release_feedback(feedback, second['released_unit_ids'],
                                 second['failure_key'])
        self.assertEqual(third['added'], [])

    def test_judge_feedback_new_failure_resets_and_solved_uncertain_release_none(self):
        first = release_feedback({'kind': 'wrong_output', 'input': 'a', 'output': 'b',
                                  'symptom': '第一个问题'})
        changed = release_feedback(
            {'kind': 'wrong_output', 'input': 'a', 'output': 'c',
             'symptom': '另一个问题'},
            first['released_unit_ids'], first['failure_key'])
        self.assertEqual(len(changed['released_unit_ids']), 1)
        self.assertEqual(changed['units'][0]['category'], 'symptom')
        self.assertEqual(release_feedback({'outcome': 'solved', 'observation': {
            'kind': 'wrong_output', 'input': 'a', 'output': 'b'}})['released_unit_ids'], [])
        self.assertEqual(release_feedback({'outcome': 'uncertain', 'observation': {
            'kind': 'wrong_output', 'input': 'a', 'output': 'b'}})['released_unit_ids'], [])

    def test_reverse_association_without_rewriting_original_content(self):
        raw=copy.deepcopy(self.raw)
        raw['items'][1]['related']=['c1']
        raw['items'][-1]['related']=[]
        before=copy.deepcopy(raw);changes=[]
        plan=validate(raw,self.issue,normalizations=changes)
        self.assertEqual(raw,before)
        self.assertEqual(plan,self.plan)
        self.assertEqual(len(changes),1)
        self.assertFalse(changes[0]['deduplicated'])
        self.assertEqual(changes[0]['canonical_from'],'c1')
        for item,original in zip(plan['items'],before['items']):
            for field in ('text','source_quote','requires','category','id'):
                self.assertEqual(item[field],original[field])

    def test_bidirectional_association_deduplicated_and_idempotent(self):
        raw=copy.deepcopy(self.raw);raw['items'][1]['related']=['c1']
        changes=[]
        plan=validate(raw,self.issue,normalizations=changes)
        self.assertEqual(plan,self.plan)
        self.assertTrue(changes[0]['deduplicated'])
        again=[]
        self.assertEqual(validate({'items':plan['items']},self.issue,normalizations=again),plan)
        self.assertEqual(again,[])
