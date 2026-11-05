"""Shared, fail-closed foreground paste/submit sequence (staging only).

The desktop driver must establish actual OS-window identity, not browser focus
emulation. This module does not provide a desktop driver or enable itself.
"""
import asyncio
import time
from dataclasses import dataclass


class InputNotSubmitted(Exception):
    """The provider submission boundary was not crossed."""


class InputOutcomeUnknown(Exception):
    """Submission may have happened; callers must not replay automatically."""


@dataclass(frozen=True)
class Target:
    account_key: str
    process_id: int
    window_id: int
    workspace: int


async def paste_and_submit(*, queue, driver, page, composer, target, prompt,
                           checkpoint, submit, timeout=60):
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
    if not isinstance(target, Target) or target.process_id <= 0 or target.window_id <= 0:
        raise InputNotSubmitted('invalid_target')
    from task_coordinator import record, preparation_safe
    try:
            # Repair only focus/pointer preparation; paste and send stay single-shot.
            for attempt in range(2):
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
                    if attempt==1:
                        raise
                    await asyncio.sleep(.2)
            # Empty contenteditable paragraphs such as <p><br></p> expose
            # a single newline through innerText; they contain no draft text.
            if await driver.composer_text(page, composer) not in ('', '\n'):
                raise InputNotSubmitted('composer_not_empty')
            record('paste_started')
            # A partial/late paste must never be repeated automatically.
            await driver.copy_prompt_and_paste(page, composer, target, prompt)
            # The paste event can arrive before the editor finishes rendering.
            # Only poll the field; never paste or submit a second time.
            deadline = time.monotonic() + 2
            while await driver.composer_text(page, composer) != prompt:
                if time.monotonic() >= deadline:
                    raise InputNotSubmitted('pasted_text_mismatch')
                if not await driver.confirm_focus(target, page, composer):
                    raise InputNotSubmitted('window_focus_lost')
                await asyncio.sleep(.05)
            if not await driver.confirm_focus(target, page, composer):
                raise InputNotSubmitted('window_focus_lost')
            from pointer_sequence import record_input_action
            record_input_action(target, 'pasted_text_verified', characters=len(prompt))
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
        from pointer_sequence import record_input_action
        record_input_action(target, 'send_action_completed')
    except BaseException as exc:
        # Includes cancellation after the ambiguous boundary.
        raise InputOutcomeUnknown('submission_outcome_unknown') from exc
