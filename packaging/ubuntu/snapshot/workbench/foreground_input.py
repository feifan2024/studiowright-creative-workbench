"""Shared, fail-closed foreground paste/submit sequence (staging only).

The desktop driver must establish actual OS-window identity, not browser focus
emulation. This module does not provide a desktop driver or enable itself.
"""
import asyncio
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path


class InputNotSubmitted(Exception):
    """The provider submission boundary was not crossed."""


class InputOutcomeUnknown(Exception):
    """Submission may have happened; callers must not replay automatically."""


async def _pacing_wait(record, account_key):
    """Same-account de-burst only. Cross-account spacing is already provided by
    the serialized foreground queue (one focused desktop window at a time), so a
    global gate would throttle without adding any real separation."""
    path = Path.home() / '.local/state/creative-unified-workbench/submit-pacing.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {}
    account_wait = (state.get('per_account') or {}).get(account_key, 0) + random.uniform(45.0, 90.0) - time.time()
    if account_wait > 0:
        record('submit_pacing_wait', seconds=round(account_wait, 1), scope='account')
        await asyncio.sleep(min(account_wait, 120.0))


async def _pointer_drift(driver, target):
    """Small real pointer movements while the window keeps focus."""
    try:
        from portal_pointer_client import PortalPointer
        pointer = PortalPointer(target, gui_environment=getattr(driver, 'env', None))
        for _ in range(random.randint(3, 6)):
            await pointer.move(random.randint(-70, 70), random.randint(-24, 24))
            await asyncio.sleep(random.uniform(.08, .22))
    except Exception:
        pass


@dataclass(frozen=True)
class Target:
    account_key: str
    process_id: int
    window_id: int
    workspace: int


async def paste_and_submit(*, queue, driver, page, composer, target, prompt,
                           checkpoint, submit, timeout=300):
    """Serialize focus, copy, paste, verification and exactly one submission.

    The caller must already hold the selected account's existing profile lock.
    It may await the provider's response only after this function returns.
    The driver must fail rather than guess when OS focus cannot be established.
    """
    async with queue.acquire_async(timeout=timeout):
        return await paste_and_submit_reserved(driver=driver, page=page, composer=composer,
            target=target, prompt=prompt, checkpoint=checkpoint, submit=submit)


async def paste_and_submit_reserved(*, driver, page, composer, target, prompt,
                                    checkpoint, submit):
    """Execute while the caller holds the shared foreground queue lease."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputNotSubmitted('invalid_prompt')
    # Chrome exposes pasted Windows line breaks as LF in the editor.
    prompt = prompt.replace('\r\n', '\n')
    if not isinstance(target, Target) or target.process_id <= 0 or target.window_id <= 0:
        raise InputNotSubmitted('invalid_target')
    from task_coordinator import record, preparation_safe
    await _pacing_wait(record, target.account_key)
    try:
            # Repair only focus/pointer preparation; paste and send stay single-shot.
            for attempt in range(3):
                if not preparation_safe():
                    raise InputNotSubmitted('submission_boundary_already_recorded')
                if await driver.composer_text(page, composer) not in ('', '\n'):
                    raise InputNotSubmitted('composer_not_empty')
                try:
                    if not await driver.activate_and_confirm(target):
                        raise InputNotSubmitted('window_activation_unconfirmed')
                    if not await driver.composer_ready(page, composer, target):
                        raise InputNotSubmitted('composer_unavailable')
                    break
                except Exception as exc:
                    record('input_preparation_recovery',attempt=attempt+1,error=type(exc).__name__)
                    if attempt==2:
                        raise
                    await asyncio.sleep(.6)
            # Empty contenteditable paragraphs such as <p><br></p> expose
            # a single newline through innerText; they contain no draft text.
            if await driver.composer_text(page, composer) not in ('', '\n'):
                raise InputNotSubmitted('composer_not_empty')
            from portal_keyboard_client import PortalKeyboard
            keyboard = PortalKeyboard(target, gui_environment=getattr(driver, 'env', None))
            typing = keyboard.ready() and len(prompt) <= 2500
            if typing:
                record('chunked_paste_started', characters=len(prompt), chunks=max(1, (len(prompt) + 47) // 48))
                await driver.copy_prompt_and_paste_chunks(page, composer, target, prompt)
            else:
                record('paste_started')
                # A partial/late paste must never be repeated automatically.
                await driver.copy_prompt_and_paste(page, composer, target, prompt)
            # The editor can lag real keystrokes; only poll, never re-enter text.
            deadline = time.monotonic() + (max(12.0, len(prompt) * 0.35) if typing else 2)
            while (await driver.composer_text(page, composer)).replace('\r\n', '\n') != prompt:
                if time.monotonic() >= deadline:
                    raise InputNotSubmitted('pasted_text_mismatch')
                if not await driver.confirm_focus(target, page, composer):
                    raise InputNotSubmitted('window_focus_lost')
                await asyncio.sleep(.05)
            if not await driver.confirm_focus(target, page, composer):
                raise InputNotSubmitted('window_focus_lost')
            from pointer_sequence import record_input_action
            record_input_action(target, 'chunked_text_verified' if typing else 'pasted_text_verified', characters=len(prompt))
            # A human looks the text over before sending; the real pointer drifts meanwhile.
            await asyncio.sleep(random.uniform(1.5, 4.0))
            await _pointer_drift(driver, target)
            # Must durably mark the boundary before invoking the submit action.
            await checkpoint()
    except InputNotSubmitted:
        raise
    except Exception as exc:
        import re
        reason=str(exc)
        reason=reason if re.fullmatch(r'[a-z_]{3,80}',reason) else type(exc).__name__
        record('input_preparation_stopped',reason=reason)
        raise InputNotSubmitted('pre_submit_failed:'+reason) from exc
    try:
        # The checkpoint may have persisted a submission boundary, so a focus
        # loss from here on is conservatively recorded as an unknown outcome.
        if not await driver.confirm_focus(target, page, composer):
            raise InputOutcomeUnknown('window_focus_lost_after_checkpoint')
        await submit()
        try:
            pacing_path = Path.home() / '.local/state/creative-unified-workbench/submit-pacing.json'
            try:
                state = json.loads(pacing_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                state = {}
            accounts = state.get('per_account') or {}
            accounts[target.account_key] = time.time()
            accounts = {k: v for k, v in accounts.items() if time.time() - v < 86400}
            pacing_path.parent.mkdir(parents=True, exist_ok=True)
            pacing_path.write_text(json.dumps({'last': time.time(), 'per_account': accounts}), encoding='utf-8')
        except OSError:
            pass
        from pointer_sequence import record_input_action
        record_input_action(target, 'send_action_completed')
    except BaseException as exc:
        # Includes cancellation after the ambiguous boundary.
        raise InputOutcomeUnknown('submission_outcome_unknown') from exc
