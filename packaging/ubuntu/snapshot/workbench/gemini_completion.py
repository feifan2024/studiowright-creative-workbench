"""Prove a completed reply after an earlier CLI capacity error; never send."""
import json
import re

RECOVERED = 'agent_response_done_after_capacity_error'


def completed_after_capacity_error(answer, steps, native=None):
    if not isinstance(answer, dict) or answer.get('status') != 'ERROR':
        return False
    error = str(answer.get('error') or '')
    if (answer.get('finished_reason') != 'error'
            or not re.search(r'\b503\b', error)
            or 'UNAVAILABLE' not in error
            or 'No capacity available for model ' not in error
            or answer.get('finish_reason') not in (None, 'STOP', 'stop', 'end_turn')):
        return False
    cid = answer.get('conversation_id')
    response = answer.get('response')
    if (not isinstance(cid, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', cid)
            or native not in (None, cid) or not isinstance(response, str) or not response.strip()
            or not steps):
        return False
    previous = -1
    errors = []
    for pos, step in enumerate(steps):
        index = step.get('step_index')
        if (step.get('conversation_id') != cid or type(index) is not int
                or index < previous or step.get('step_type') not in ('user_input', 'error_message', 'agent_response')):
            return False
        previous = index
        if step.get('step_type') == 'error_message':
            if step.get('state') != 'DONE':
                return False
            errors.append(pos)
    if not errors:
        return False
    tail = steps[errors[-1] + 1:]
    # No earlier partial reply, second turn, tool execution, or error after DONE.
    if any(s.get('step_type') == 'agent_response' for s in steps[:errors[-1] + 1]):
        return False
    if (not tail or tail[-1].get('state') != 'DONE'
            or tail[0].get('step_index') <= steps[errors[-1]]['step_index']):
        return False
    text = []
    for pos, step in enumerate(tail):
        if (step.get('step_type') != 'agent_response' or step.get('step_index') != tail[0]['step_index']
                or step.get('state') != ('DONE' if pos == len(tail) - 1 else 'ACTIVE')):
            return False
        delta = step.get('text_delta', '')
        if not isinstance(delta, str):
            return False
        text.append(delta)
    return ''.join(text) == response


def saved_capacity_completion(audit, result, native, read_private):
    """Derive a corrected view while keeping the original receipt immutable."""
    if (result.get('status') != 'unknown' or result.get('reason') != 'provider_unavailable'
            or result.get('submitted') is not True or result.get('timeout_kind') is not None
            or result.get('returncode') not in (None, 0, 1)):
        return result
    steps = []
    answer = None
    previous_ms = -1
    for line in read_private(audit / 'events.jsonl', 8 * 1024 * 1024).decode('utf-8').splitlines():
        record = json.loads(line)
        ms = record.get('elapsed_ms')
        if type(ms) not in (int, float) or not previous_ms <= ms <= result['total_ms'] + 1000:
            return result
        previous_ms = ms
        if record.get('channel') == 'stderr':
            if re.search(r'\bretrying\b|\bretry attempt\b|switching.*model|falling back', record.get('line', ''), re.I):
                return result
            continue
        if record.get('channel') != 'stdout':
            return result
        event = json.loads(record['line'])
        if answer is not None:
            return result
        if event.get('event') == 'step_update':
            steps.append(event['step_update'])
        elif event.get('event') == 'result':
            answer = event['result']
    if (not completed_after_capacity_error(answer, steps, native)
            or result.get('response') != answer['response']):
        return result
    return {**result, 'status': 'completed', 'reason': None,
            'native': answer['conversation_id'], 'completion_source': RECOVERED,
            'provider_warning': 'recovered_capacity_503'}
