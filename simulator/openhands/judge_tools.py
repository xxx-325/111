"""Judge-only submission tool. Reuses the SDK host tool executor."""
from typing import Literal
from pydantic import Field
from openhands.sdk import Action
from openhands.sdk.tool import ToolDefinition, register_tool
from .control_tools import definition


class VerdictAction(Action):
    outcome: Literal['solved', 'unsolved', 'uncertain']
    reason: str = Field(description='Private reasoning summary grounded in current issue and actual observations')
    feedback: str = Field(default='', description='Required short user-observable symptom for an unsolved verdict; never a test name, path, root cause, or fix')
    feedback_detail: str = Field(default='', description='Optional concrete input, actual output/error, or usage condition; never private test details or implementation advice')
    requested_fragment_id: str = Field(default='',
        description='One hidden issue fragment needed to answer a current Code question; otherwise empty.')


class SubmitVerdictTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(cls, VerdictAction, 'judge_verdict',
            'Submit the current candidate verdict. An observed required-behavior violation is unsolved; uncertain is only for missing key evidence, an environment block, or conflicting evidence.')


class VerdictRevisionAction(Action):
    outcome: Literal['solved', 'unsolved', 'uncertain']
    reason: str = Field(description='Corrected private conclusion using only the existing observations')
    feedback: str = Field(default='', description='Short observable symptom required when the corrected outcome is unsolved')
    feedback_detail: str = Field(default='', description='Optional concrete input, actual result/error, or usage condition')


class ReviseVerdictTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(cls, VerdictRevisionAction, 'judge_verdict_revision',
            'Correct only outcome, reason, and matching feedback from existing evidence. Do not inspect again.')


class FeedbackRevisionAction(Action):
    feedback: str = Field(default='', description='Replacement short observable symptom; empty uses the latest reviewed symptom')
    feedback_detail: str = Field(default='', description='Replacement concrete input, actual result/error, or usage condition')


class ReviseFeedbackTool(ToolDefinition):
    @classmethod
    def create(cls, conv_state, **kwargs):
        return definition(cls, FeedbackRevisionAction, 'judge_feedback_revision',
            'Correct only the public feedback. The host keeps the verdict and evidence.')


JUDGE_TOOLS = [SubmitVerdictTool, ReviseVerdictTool, ReviseFeedbackTool]
for tool in JUDGE_TOOLS:
    register_tool(tool.name, tool)
