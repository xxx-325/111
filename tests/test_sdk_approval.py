"""No-provider SDK approval fixture; not generated dialogue evidence."""
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from openhands.sdk import Agent, Conversation, LLM, Message, TextContent
from openhands.sdk.llm.message import MessageToolCall
from openhands.sdk.security.confirmation_policy import AlwaysConfirm
from openhands.sdk.tool import Tool
from openhands.tools.task_tracker import TaskTrackerTool


class SDKApprovalTests(unittest.TestCase):
    def test_reject_before_execution_and_approve_once(self):
        with tempfile.TemporaryDirectory() as root:
            events=[]
            agent=Agent(llm=LLM(model='openai/test',api_key='fixture',max_input_tokens=65536),
                tools=[Tool(name=TaskTrackerTool.name)],include_default_tools=[],system_prompt='Fixture')
            conversation=Conversation(agent=agent,workspace=root,persistence_dir=root+'/sdk',
                callbacks=[events.append],visualizer=None)
            conversation.set_confirmation_policy(AlwaysConfirm())
            conversation.send_message('Fixture')
            tool=conversation.agent.tools_map['task_tracker']
            calls=[]
            original=type(tool.executor).__call__
            def execute(executor,action,conversation=None):
                calls.append(action.command)
                result=original(executor,action,conversation)
                conversation.pause()
                return result
            def response(identifier):
                return SimpleNamespace(id=identifier,message=Message(role='assistant',content=[],tool_calls=[
                    MessageToolCall(id=identifier,name='task_tracker',arguments='{"command":"view"}',origin='completion')]))
            with patch('openhands.sdk.agent.agent.make_llm_completion',side_effect=[response('one'),response('two')]), patch.object(type(tool.executor),'__call__',execute):
                conversation.run()
                self.assertIn('WAITING_FOR_CONFIRMATION',str(conversation.state.execution_status))
                self.assertEqual(calls,[])
                conversation.reject_pending_actions('Denied fixture')
                self.assertEqual(calls,[])
                conversation.run()
                self.assertEqual(calls,[])
                conversation.run()
                self.assertEqual(calls,['view'])
            self.assertTrue(any(e.__class__.__name__=='UserRejectObservation' for e in events))
            conversation.close()
