"""Private Judge protocol, immutable candidate identity and feedback review."""
import hashlib
import json
import re
import shlex
from pathlib import Path
from .issue_stages import call_json
from .provenance import collect_sources, source_check
from .feedback_projection import (PublicFeedbackError, authorized_feedback_values,
                                  authorized_raw_values,
                                  project_latest_feedback,
                                  validate_public_feedback)
from ..episode import obvious_leak

SYSTEM = '''你是 Judge。检查完整 issue 在只读 /workspace/candidate 中是否解决；参考资料在 /reference，测试写入 /workspace/checks。
已观察到候选违反完整 issue 必需行为时必须判 unsolved，即使根因或修法未知。uncertain 仅用于判定所需的关键证据缺失、环境阻塞或证据冲突，不能代替已观察失败。
失败时提交一句简短、用户可观察的 feedback；可选 feedback_detail 只写具体输入、实际输出/报错或使用条件。完整输入和实际结果/报错必须来自同一条闭合终端输出：INPUT/RESULT 或 INPUT/ERROR；不要写测试名、私有路径、源码根因或修复建议，这些留在私有 reason。'''


_DIRECT_VERIFIERS = {
    'pytest', 'py.test', 'sphinx-build', 'tox', 'nox', 'jest', 'vitest',
    'mocha', 'rspec', 'ctest',
}
_SCRIPT_RUNNERS = {'npm', 'pnpm', 'yarn', 'bun'}
_SCRIPT_TARGET = re.compile(r'(?:^|[-_:])(test|check|build|docs?)(?:$|[-_:])')
_MAKE_TARGETS = {'all', 'test', 'tests', 'check', 'build', 'docs', 'doc', 'html'}
_SHELL_SEPARATORS = {'&&', '||', ';'}


def _is_validation_command(command):
    try:
        tokens = shlex.split(command.removeprefix('[RESET] '))
    except ValueError:
        return False
    starts = [0] + [
        index + 1 for index, token in enumerate(tokens) if token in _SHELL_SEPARATORS
    ]
    for start in starts:
        index = start
        while index < len(tokens) and (
            re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tokens[index])
            or tokens[index] in {'command', 'sudo', 'env'}
        ):
            index += 1
        if index >= len(tokens):
            continue
        executable = Path(tokens[index]).name
        remainder = tokens[index:]
        if executable in _DIRECT_VERIFIERS:
            return True
        if executable in {'python', 'python3'} or executable.startswith('python3.'):
            if len(remainder) >= 3 and remainder[1:3] in (
                ['-m', 'pytest'], ['-m', 'unittest'], ['-m', 'sphinx']
            ):
                return True
        if executable in _SCRIPT_RUNNERS and len(remainder) >= 2:
            target_index = 2 if remainder[1] == 'run' and len(remainder) >= 3 else 1
            if target_index < len(remainder) and _SCRIPT_TARGET.search(
                remainder[target_index].casefold()
            ):
                return True
        if executable in {'make', 'gmake'} and any(
            token.casefold() in _MAKE_TARGETS for token in remainder[1:]
            if not token.startswith('-')
        ):
            return True
        if executable == 'go' and len(remainder) >= 2 and remainder[1] == 'test':
            return True
        if executable == 'cargo' and len(remainder) >= 2 and remainder[1] in {
            'test', 'check', 'build'
        }:
            return True
        if executable in {'mvn', 'mvnw', 'gradle', 'gradlew'} and any(
            token.casefold() in {'test', 'check', 'verify', 'package', 'build'}
            for token in remainder[1:] if not token.startswith('-')
        ):
            return True
    return False


def _has_pipeline(command):
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars='|')
        lexer.whitespace_split = True
        return any(token == '|' for token in lexer)
    except ValueError:
        return True


def _terminal_command_and_exit(observation):
    if observation.get('tool') != 'terminal':
        return None, None
    payload = observation.get('observation') or {}
    if not isinstance(payload, dict):
        return None, None
    exit_code = observation.get('exit_code', payload.get('exit_code'))
    if exit_code is None and isinstance(payload.get('metadata'), dict):
        exit_code = payload['metadata'].get('exit_code')
    return payload.get('command'), exit_code


_PYTEST_OPTIONS_WITH_VALUES = {
    '-o', '-p', '--basetemp', '--capture', '--color', '--confcutdir',
    '--import-mode', '--junitxml', '--maxfail', '--override-ini',
    '--rootdir', '--tb',
}
_PYTEST_FILTERS_WITH_VALUES = {
    '-k', '-m', '--deselect', '--ignore', '--ignore-glob',
}
_PYTEST_FILTER_FLAGS = {
    '--collect-only', '--failed-first', '--ff', '--last-failed', '--lf',
    '--new-first', '--nf', '--pyargs', '--stepwise', '--sw',
}


def _pytest_scope(command):
    """Return pytest's selected positional targets and scope filters."""
    try:
        tokens = shlex.split(command.removeprefix('[RESET] '))
    except ValueError:
        return None
    invocation = None
    for index, token in enumerate(tokens):
        executable = Path(token).name
        if executable in {'pytest', 'py.test'}:
            invocation = index + 1
            break
        if (executable in {'python', 'python3'} or executable.startswith('python3.')):
            if tokens[index + 1:index + 3] == ['-m', 'pytest']:
                invocation = index + 3
                break
    if invocation is None:
        return None
    args = []
    for token in tokens[invocation:]:
        if token in _SHELL_SEPARATORS or token == '|':
            break
        args.append(token)
    targets = []
    filters = []
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split('=', 1)[0]
        if option in _PYTEST_FILTERS_WITH_VALUES:
            if '=' in token:
                filters.append((option, token.split('=', 1)[1]))
                index += 1
            elif index + 1 < len(args):
                filters.append((option, args[index + 1]))
                index += 2
            else:
                filters.append((option, ''))
                index += 1
            continue
        if option in _PYTEST_FILTER_FLAGS:
            filters.append((option, ''))
            index += 1
            continue
        if option in _PYTEST_OPTIONS_WITH_VALUES:
            index += 1 if '=' in token else 2
            continue
        if token.startswith('-') or re.match(r'^\d*>', token):
            index += 1
            continue
        targets.append(token)
        index += 1
    return tuple(sorted(targets)), tuple(sorted(filters))


def _trusted_validation_success(observation, command, exit_code):
    return exit_code == 0 and (
        not _has_pipeline(command)
        or observation.get('pipeline_exit_policy') == 'pipefail'
    )


def _validate_solved_terminal_evidence(payload, observations):
    if payload.get('outcome') != 'solved':
        return
    selected = set(payload.get('evidence_ids', []))
    selected_observations = [
        observation for observation in observations
        if observation.get('id') in selected
    ]
    for index, observation in enumerate(selected_observations):
        command, exit_code = _terminal_command_and_exit(observation)
        if not isinstance(command, str):
            continue
        if not _is_validation_command(command):
            continue
        if exit_code != 0:
            failure_scope = _pytest_scope(command)
            superseded = failure_scope is not None and any(
                later_scope in (failure_scope, ((), ()))
                and _trusted_validation_success(later, later_command, later_exit)
                for later in selected_observations[index + 1:]
                for later_command, later_exit in [_terminal_command_and_exit(later)]
                if isinstance(later_command, str)
                for later_scope in [_pytest_scope(later_command)]
            )
            if not superseded:
                raise ValueError('solved requires a successful validation exit')
            continue
        if _has_pipeline(command) and observation.get(
            'pipeline_exit_policy'
        ) != 'pipefail':
            raise ValueError('solved validation pipeline has no trusted exit policy')


def assessment_basis(mode):
    if mode == 'static_reference':
        return '本次是流程实验，允许依据候选代码、文档和参考修改作静态判断，不要求安装环境或执行完整测试。结论说明静态依据和未运行部分，不把静态推断写成实际报错或已测试。'
    if mode != 'default':
        raise ValueError('unknown verification mode')
    return '依据当前候选的实际检查判断。'


def bind_submission(submitted, job, observations, *, project_feedback=True):
    """Attach identity, evidence and raw feedback that only the host can know."""
    payload = dict(submitted)
    requested = payload.pop('requested_fragment_id', '')
    summary = payload.pop('feedback', '')
    detail = payload.pop('feedback_detail', '')
    payload['task_id'] = job['task_id']
    payload['candidate_version'] = job['candidate_version']
    payload['evidence_ids'] = [item['id'] for item in observations]
    payload['public_feedback'] = ({}
        if payload.get('outcome') == 'solved' or not project_feedback
        else project_latest_feedback(observations, detail, summary))
    payload['requested_fragment_ids'] = [requested] if requested else []
    return payload


def candidate_hash(root):
    """Hash candidate content and behavior-relevant file metadata."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('candidate root must be an existing directory')
    result = hashlib.sha256(b'candidate-tree-v2\0')
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('unsafe candidate entry')
        relative = str(path.relative_to(root)).encode()
        mode = path.stat().st_mode & 0o7777
        result.update(relative + b'\0' + (b'd' if path.is_dir() else b'f'))
        result.update(f'\0{mode:o}\0'.encode())
        if path.is_file():
            result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def validate_verdict_core(payload, job, observations, plan):
    if payload.get('task_id') != job['task_id'] or payload.get('candidate_version') != job['candidate_version']:
        raise ValueError('stale Judge task or candidate')
    if payload.get('outcome') not in ('solved', 'unsolved', 'uncertain') or not payload.get('reason'):
        raise ValueError('invalid Judge outcome')
    known = {item['id'] for item in observations}
    ids = payload.get('evidence_ids')
    if not isinstance(ids, list) or any(i not in known for i in ids):
        raise ValueError('Judge evidence must reference current candidate observations')
    if payload['outcome'] != 'uncertain' and not ids:
        raise ValueError('solved/unsolved requires actual inspection evidence')
    _validate_solved_terminal_evidence(payload, observations)
    if 'clarification_stage' in payload:
        raise ValueError('old stage protocol is not supported')
    requested=payload.get('requested_fragment_ids',[])
    known={item['id'] for item in plan['items']}
    if not isinstance(requested,list) or any(not isinstance(i,str) or i not in known for i in requested) or len(requested)!=len(set(requested)):
        raise ValueError('invalid requested fragments')
    if requested and payload['outcome']=='solved':
        raise ValueError('solved verdict cannot request more issue information')


def validate_verdict(payload, job, observations, plan):
    validate_verdict_core(payload, job, observations, plan)
    feedback = validate_public_feedback(payload.get('public_feedback', {}), observations,
                                        payload.get('evidence_ids', []))
    if payload['outcome'] == 'unsolved' and not feedback:
        raise PublicFeedbackError('unsolved requires observable public feedback')
    if payload['outcome'] == 'unsolved' and not feedback.get('symptom'):
        raise PublicFeedbackError('unsolved requires a short observable feedback symptom')
    if payload['outcome'] == 'solved' and feedback:
        raise PublicFeedbackError('solved verdict must not expose public feedback')


def review_verdict(relay, task, job, payload, events, observations, visible, plan, public_history=()):
    """A read-only audit, not another autonomous agent or a rewriting step."""
    feedback = payload.get('public_feedback', {})
    feedback_values = authorized_feedback_values(feedback)
    text = json.dumps(feedback, ensure_ascii=False)
    # Inspect field values before JSON encoding so quotes in copied patch lines
    # cannot hide reference implementation text behind escape characters.
    leak_text = '\n'.join(feedback_values) or text
    leak = obvious_leak(leak_text, [task],
        deferred_reference_test_values=authorized_raw_values(feedback))
    leaks = [leak] if leak else []
    if any(word in text for word in ('/reference', '/workspace/checks', '/workspace/experiments', job['candidate_version'])):
        leaks.append('private location or candidate identifier')
    sources = collect_sources([], events)
    overlap = source_check(leak_text, sources, visible, [])
    review = call_json(relay,
        '审核 Judge 结论，返回 JSON {verdict_valid:boolean,grounded:boolean,feedback_safe:boolean,required_failure_observed:boolean,reasons:[string]}。'
        '观察已显示候选违反完整 issue 必需行为时，uncertain 无效，应为 unsolved；uncertain 仅在关键证据缺失、环境阻塞或证据冲突时有效。'
        'required_failure_observed 表示当前观察是否已经证明任一必需行为不满足；它为 true 时 outcome 必须是 unsolved，unsolved 时它也必须为 true。'
        '若提供 scenario，合成事实只按其 scope 判断，不伪称为原提交事实。请求释放受控片段时，核对该片段 trigger 是否由当前提问或真实观察满足，未满足则 verdict_valid=false；不能只因轮次增加而释放。'
        '未触发且未释放的受控事实不能绕过片段释放而写进公开 feedback，否则 feedback_safe=false。'
        '结论必须由当前候选的观察支持。feedback 是短的用户真实可见结果；feedback_detail 只能是具体输入、实际输出/报错或使用条件。两者都不能含测试名/路径、源码根因、修复建议或未观察事实；reason 保持私有。闭合运行块中的 INPUT/RESULT/ERROR 必须完整且来自候选实际运行。不要改写。',
        dict(issue={k: task[k] for k in ('title', 'body')}, code_reply=job['code_reply'],
             scenario=task.get('scenario_context', {}),
             public_turn=job.get('public_turn', []),
             assessment_basis=assessment_basis(task.get('verification_mode','default')),
             verdict=payload, observations=observations, released_requirement=visible,
             fragments=[{'id': item['id'], 'category': item['category'], 'text': item['text']}
                        for item in plan['items']], public_history=public_history))
    required_failure = review.get('required_failure_observed')
    outcome_consistent = isinstance(required_failure, bool) and (
        (payload.get('outcome') == 'unsolved') == required_failure
    )
    verdict_valid = review.get('verdict_valid') is True and outcome_consistent
    conclusion_valid = verdict_valid and review.get('grounded') is True
    feedback_safe = review.get('feedback_safe') is True and not leaks
    reasons = list(leaks)
    if not conclusion_valid:
        reasons.extend(
            reason for reason in review.get('reasons', [])
            if isinstance(reason, str) and reason
        )
        reasons.append('Judge conclusion audit did not pass')
    if not outcome_consistent:
        reasons.append('Judge outcome conflicts with observed required-behavior failure')
    return dict(allowed=conclusion_valid and feedback_safe, conclusion_valid=conclusion_valid,
                verdict_valid=verdict_valid,
                grounded=review.get('grounded') is True,
                feedback_safe=feedback_safe, reasons=reasons, semantic=review, source_check=overlap)
