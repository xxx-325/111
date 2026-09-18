import copy
import json
from pathlib import Path
import tempfile
import unittest
from simulator.expression import replay_messages
from simulator.replay import run
from simulator.replay_grounding import validate_task, response_messages, check_decision, task_messages


HISTORY = [{'role':'user','text':'Run evaluation.'}, {'role':'assistant','text':'Quota exhausted. Restore quota first.'}]
FACT = {'text':'Evaluate','source':0,'quote':'Run evaluation.'}
RAW = dict(current_goal=FACT, task_state='EVALUATE', reported_done=[], open_issues=[],
           blockers=[{'text':'Restore quota before execution','source':1,'quote':'Quota exhausted. Restore quota first.'}], questions=[])


def decision(**kwargs):
    result = dict(next_task_state='EVALUATE', control='CONTINUE', change_quote='', execution='none',
                  condition_quote='', completion='none', reason='Continue the evaluation task')
    result.update(kwargs)
    return result


class GroundingTests(unittest.TestCase):
    def test_exact_evidence_and_authority(self):
        task = validate_task(RAW, HISTORY)
        self.assertEqual(task['blockers'][0]['authority'], 'assistant_report')
        self.assertEqual(task['independent_completion'], 'unknown')
        for edit in ({'quote':'FUTURE_CANARY'}, {'source':2}, {'authority':'execution_proof'}):
            bad=copy.deepcopy(RAW);bad['blockers'][0].update(edit)
            with self.assertRaises(ValueError): validate_task(bad,HISTORY)
        with self.assertRaises(ValueError): validate_task(RAW,[HISTORY[0],dict(role='context_note',text=HISTORY[1]['text'])])

    def test_inputs_only_add_task_and_conditions(self):
        for enabled in (False, True):
            original=replay_messages(HISTORY,enabled)
            new=response_messages(HISTORY,enabled,validate_task(RAW,HISTORY))
            self.assertEqual(original[0],new[0])
            p=json.loads(new[1]['content']);p.pop('task_record');p.pop('action_conditions')
            self.assertEqual(p,json.loads(original[1]['content']))
        self.assertNotIn('FUTURE_CANARY',json.dumps(task_messages(HISTORY)))

    def test_continue_preserves_task_and_blocker(self):
        task=validate_task(RAW,HISTORY)
        p=dict(action='advance',message='Wait for quota to recover.',decision=decision())
        result=check_decision(p,task)
        self.assertFalse(result['changed']);self.assertEqual(result['next_state'],'EVALUATE')
        p['decision']=decision(execution='immediate')
        self.assertIn('execution_while_blocked',check_decision(p,task)['violations'])
        p['decision']=decision(execution='after_recovery',condition_quote='quota to recover')
        self.assertFalse(check_decision(p,task)['violations'])
        p['decision']=decision(completion='independent')
        self.assertIn('unsupported_independent_verification',check_decision(p,task)['violations'])
        p['decision']=decision(next_task_state='BUILD')
        self.assertIn('unsupported_task_transition',check_decision(p,task)['violations'])

    def test_task_failure_cached_and_in_denominator(self):
        calls=[]
        class Fake:
            def __init__(self,config): pass
            def complete(self,messages,tools):
                calls.append(messages)
                if 'Extract the current task' in messages[0]['content']: return {'content':'bad task JSON'}
                return {'content':'{"action":"check","message":"检查一下"}'}
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'run'
            rows=run([dict(id='x',history=HISTORY)],{},out,Fake,modes=('baseline','grounded'))
            self.assertEqual(len(rows),8);self.assertEqual(len(calls),5)
            self.assertEqual(sum(r['status']=='failed' for r in rows),4)
            self.assertEqual(json.loads((out/'task-records.json').read_text())['x']['status'],'failed')

    def test_unknown_question_and_pause_do_not_imply_completion(self):
        raw=copy.deepcopy(RAW);raw['questions']=[{'text':'Choose a plan','source':1,'quote':'Restore quota first.'}]
        task=validate_task(raw,HISTORY)
        packet=json.loads(response_messages(HISTORY,False,task)[1]['content'])
        self.assertIn('answer_or_disclose_unknown',str(packet['action_conditions']))
        result=check_decision(dict(action='finish',message='Pause for now',decision=decision()),task)
        self.assertFalse(result['violations'])

    def test_successful_task_is_cached_across_four_drafts(self):
        calls=[]
        class Fake:
            def __init__(self,config): pass
            def complete(self,messages,tools):
                self_tools=tools;calls.append((messages,self_tools))
                if 'Extract the current task' in messages[0]['content']: return {'content':json.dumps(RAW)}
                return {'content':json.dumps(dict(action='advance',message='Wait for quota to recover.',decision=decision()))}
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'run'
            rows=run([dict(id='x',history=HISTORY)],{},out,Fake,modes=('baseline','grounded'))
            self.assertEqual(len(calls),9)
            self.assertTrue(all(not tool for _,tool in calls))
            self.assertTrue(all(r['status']=='generated' for r in rows))
            self.assertEqual(sum('transition' in r for r in rows),4)
            self.assertTrue(all(r['transition']['current_state']=='EVALUATE' for r in rows if 'transition' in r))

    def test_preparation_interruption_preserves_every_planned_slot(self):
        class Interrupt:
            def __init__(self,config): pass
            def complete(self,messages,tools): raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'run'
            with self.assertRaises(KeyboardInterrupt): run([dict(id='x',history=HISTORY)],{},out,Interrupt,modes=('baseline','grounded'))
            self.assertEqual(len(json.loads((out/'results.json').read_text())),8)
            self.assertEqual(json.loads((out/'task-records.json').read_text())['x']['status'],'pending')


if __name__=='__main__': unittest.main()
