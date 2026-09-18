"""Pinned upstream role text with local language and tool integration."""
import hashlib
import json
from pathlib import Path

PROMPTS = Path(__file__).parent/'prompts/tom_swe_user.json'
LANGUAGES = {'zh-CN': '公开对话使用简体中文，代码、标识符和原始报错保持原文。',
             'en': 'Use English for public dialogue; preserve code, identifiers and original error messages.'}


def role_prompts(language, code_prompt_mode='local'):
    if code_prompt_mode not in ('local', 'sdk_default'):
        raise ValueError('unknown Code prompt mode')
    if language not in LANGUAGES:
        raise ValueError('dialogue_language must be zh-CN or en')
    user = ('你是与 Code Agent 对话的用户，只提出当前问题、回答澄清或反馈结果；Code 负责实现。'
            '只使用当前信息，不读源码，不补造细节。用控制工具推进或结束。\n' + LANGUAGES[language])
    code = '你是代码助手。按用户需求检查、修改并验证 /workspace/candidate 中的代码；环境离线，一轮结束如实说明结果或待回答的问题。\n' + LANGUAGES[language]
    return {'user': user, 'code': None if code_prompt_mode == 'sdk_default' else code}


def policy_record(language, variant='neutral', code_prompt_mode='local'):
    if variant not in ('baseline', 'delegate', 'neutral'):
        raise ValueError('unknown delegation variant')
    prompts = role_prompts(language, code_prompt_mode)
    return dict(version='delegating-v25-cross-task-final-evidence', dialogue_language=language, delegation_variant=variant,
                local_adaptation='User has dialogue controls only; Code and Judge retain SDK development tools.',
                implementation_sha256={name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                    for name in ('tool_wording.py', 'episode.py', 'events.py', 'user_final_fallback.py', 'container.py', 'transition_selection.py', 'state.py', 'provenance.py', 'guard.py', 'evidence_links.py','permissions.py','worker.py','user_projection.py','simulated_experience.py','feedback_projection.py', 'sandbox.py', 'remote_tools.py', 'remote_safety.py', 'relay.py', 'control_tools.py', '../episode.py', '../state_machine.py', '../native_http.py')},
                upstream=json.loads(PROMPTS.read_text()),
                code_prompt_mode=code_prompt_mode,
                prompt_sha256={role: hashlib.sha256(text.encode()).hexdigest() if text is not None else None for role, text in prompts.items()})
