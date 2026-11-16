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


def choose_prompt_input(prompt):
    import re
    if re.fullmatch(r'[A-Za-z0-9 ,.]{1,120}', prompt) and random.randrange(2) == 0:
        return 'type'
    return 'cdp_type'


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


async def enter_hybrid_prompt(driver, page, composer, target, prompt):
    from task_coordinator import record
    if await driver.composer_text(page, composer) not in ('', '\n'):
        raise InputNotSubmitted('composer_not_empty')
    if not await driver.confirm_focus(target, page, composer):
        raise InputNotSubmitted('window_focus_lost')
    prefix = prompt[:min(3, len(prompt))]
    record('hybrid_input_phase', stage='typing_prefix', characters=len(prompt))
    await driver.type_prompt_chars(page, prefix)
    if len(prompt) > len(prefix):
        if not await driver.confirm_focus(target, page, composer):
            raise InputNotSubmitted('window_focus_lost')
        # Use the browser's existing text-input route directly, once, instead of
        # a desktop clipboard shortcut whose acknowledgement may insert nothing.
        # Focus and exact-content verification still guard the submit boundary.
        record('hybrid_input_phase', stage='insert_start', characters=len(prompt))
        await page.keyboard.insert_text(prompt[len(prefix):])
        record('hybrid_input_phase', stage='insert_finished', characters=len(prompt))
    observed = (await driver.composer_text(page, composer)).replace('\r\n', '\n')
    if observed != prompt:
        # Record lengths only; prompt and clipboard content never enter logs.
        record('hybrid_input_verification_pending', expected_characters=len(prompt),
               observed_characters=len(observed), prefix_matches=observed.startswith(prefix))
    while observed != prompt:
        if not await driver.confirm_focus(target, page, composer):
            raise InputNotSubmitted('window_focus_lost')
        await asyncio.sleep(.05)
        observed = (await driver.composer_text(page, composer)).replace('\r\n', '\n')
    if not await driver.confirm_focus(target, page, composer):
        raise InputNotSubmitted('window_focus_lost')


def hybrid_input_timeout(prompt):
    """Allow desktop focus, paste and DOM reconciliation time for long prompts."""
    return min(45.0, max(15.0, 10.0 + len(prompt) / 100.0))


@dataclass(frozen=True)
class Target:
    account_key: str
    process_id: int
    window_id: int
    workspace: int


async def paste_and_submit(*, queue, driver, page, composer, target, prompt,
                           checkpoint, submit, timeout=300, hybrid=False):
    """Serialize focus, copy, paste, verification and exactly one submission.

    The caller must already hold the selected account's existing profile lock.
    It may await the provider's response only after this function returns.
    The driver must fail rather than guess when OS focus cannot be established.
    """
    async with queue.acquire_async(timeout=timeout):
        return await paste_and_submit_reserved(driver=driver, page=page, composer=composer,
            target=target, prompt=prompt, checkpoint=checkpoint, submit=submit, hybrid=hybrid)


async def paste_and_submit_reserved(*, driver, page, composer, target, prompt,
                                    checkpoint, submit, hybrid=False):
    """Execute while the caller holds the shared foreground queue lease."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise InputNotSubmitted('invalid_prompt')
    # Chrome exposes pasted Windows line breaks as LF in the editor.
    prompt = prompt.replace('\r\n', '\n')
    if not isinstance(target, Target) or target.process_id <= 0 or target.window_id <= 0:
        raise InputNotSubmitted('invalid_target')
    from task_coordinator import record, preparation_safe
    await _pacing_wait(record, target.account_key)
    mode = "hybrid" if hybrid else choose_prompt_input(prompt)
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
                    ready = await (driver.composer_ready_cdp(page, composer, target)
                                   if mode in ('cdp_type', 'hybrid')
                                   else driver.composer_ready(page, composer, target))
                    if not ready:
                        raise InputNotSubmitted('composer_unavailable')
                    break
                except Exception as exc:
                    record('input_preparation_recovery',attempt=attempt+1,error=type(exc).__name__)
                    if attempt==2:
                        raise
                    await asyncio.sleep(.6)
            if hybrid:
                try:
                    record('hybrid_input_timeout_budget', seconds=hybrid_input_timeout(prompt), characters=len(prompt))
                    await asyncio.wait_for(
                        enter_hybrid_prompt(driver, page, composer, target, prompt),
                        timeout=hybrid_input_timeout(prompt),
                    )
                    record('hybrid_input_verified', characters=len(prompt))
                except asyncio.TimeoutError as exc:
                    raise InputNotSubmitted('prompt_input_timeout') from exc
            else:
                # Empty contenteditable paragraphs such as <p><br></p> expose
                # a single newline through innerText; they contain no draft text.
                if await driver.composer_text(page, composer) not in ('', '\n'):
                    raise InputNotSubmitted('composer_not_empty')
                from portal_keyboard_client import PortalKeyboard
                keyboard = PortalKeyboard(target, gui_environment=getattr(driver, 'env', None))
                # Short, single-line ASCII may use the portal keyboard; everything
                # else types per character over CDP, falling back to a single
                # chunked paste if the CDP path fails.
                typing = keyboard.ready() and mode == 'type'
                record('typing_started' if typing else (mode + '_started'), characters=len(prompt))
                if typing:
                    if not await driver.confirm_focus(target, page, composer):
                        raise InputNotSubmitted('window_focus_lost')
                    await keyboard.type(prompt)
                elif mode == 'cdp_type':
                    try:
                        await driver.type_prompt_chars(page, prompt)
                    except Exception as exc:
                        record('cdp_typing_fallback', error=type(exc).__name__)
                        if not await driver.confirm_focus(target, page, composer):
                            raise InputNotSubmitted('window_focus_lost')
                        await driver.clear_composer(page, composer)
                        await driver.copy_prompt_and_paste(page, composer, target, prompt)
                else:
                    await driver.copy_prompt_and_paste(page, composer, target, prompt)
                # Only poll; never repeat a partially entered prompt.
                deadline = time.monotonic() + 2
                while (await driver.composer_text(page, composer)).replace('\r\n', '\n') != prompt:
                    if time.monotonic() >= deadline:
                        raise InputNotSubmitted('pasted_text_mismatch')
                    if not await driver.confirm_focus(target, page, composer):
                        raise InputNotSubmitted('window_focus_lost')
                    await asyncio.sleep(.05)
                if not await driver.confirm_focus(target, page, composer):
                    raise InputNotSubmitted('window_focus_lost')
                from pointer_sequence import record_input_action
                record_input_action(target, 'typed_text_verified' if typing else ('cdp_typed_verified' if mode == 'cdp_type' else 'pasted_text_verified'), characters=len(prompt))
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
