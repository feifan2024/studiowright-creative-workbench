"""Fail-closed desktop pointer preparation; caller owns the foreground queue."""
import asyncio
import random
import time
import json
import os
from pathlib import Path

INPUT_SEQUENCE_VERSION = "motion-500ms-two-clicks-20261105"

def record_input_action(target, action, **extra):
    root = Path.home()/".local/state/creative-unified-workbench/desktop-pointer"
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise PointerPreparationError("input_audit_unavailable")
    path = root/"input-actions.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os,"O_NOFOLLOW",0), 0o600)
    event = {"time":time.time(),"version":INPUT_SEQUENCE_VERSION,"account_key":target.account_key,
             "window":target.window_id,"workspace":target.workspace,"action":action,**extra}
    with os.fdopen(fd,"a",encoding="utf-8") as stream:
        stream.write(json.dumps(event,separators=(",",":"))+"\n")

FOCUS_MOTION_SECONDS = .50
FOCUS_MOTION_STEPS = 10
FOCUS_CLICKS = 2


def focus_path(sx, sy, width, height, rng):
    """Bounded random curve from the known pointer anchor to the editor.

    Only move here; an independently verified editor hit is required to click.
    This is input focus preparation, not proof of human verification.
    """
    ax, ay = width - 1, height - 1
    dx, dy = sx - ax, sy - ay
    distance = max(1, (dx * dx + dy * dy) ** .5)
    bend = rng.uniform(-1, 1) * min(80, distance * .08)
    offset_x, offset_y = -dy / distance * bend, dx / distance * bend
    points = []
    for step in range(1, FOCUS_MOTION_STEPS + 1):
        t = step / FOCUS_MOTION_STEPS
        arc = 4 * t * (1 - t)
        x = min(width - 3, max(2, ax + dx * t + offset_x * arc))
        y = min(height - 3, max(2, ay + dy * t + offset_y * arc))
        points.append((x, y))
    points[-1] = (sx, sy)
    return points


class PointerPreparationError(Exception):
    pass


async def prepare_pointer(*, page, composer, target, driver, pointer, rng=None):
    """Move the OS pointer, scroll, click the input, and confirm focus.

    The provider is never submitted from here. Pointer input is supplied by an
    already authorised desktop session. A DOM mouse observation guards against
    inaccurate window decorations, display scale, or multiple monitors.
    """
    rng = rng or random.SystemRandom()
    if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
        raise PointerPreparationError('window_focus_unconfirmed')
    await page.evaluate('''() => {
        if (window.__swPointerObserver) window.removeEventListener('pointermove', window.__swPointerObserver, true);
        window.__swPointerLast = null;
        window.__swPointerObserver = e => { if (e.isTrusted) window.__swPointerLast = {x:e.clientX, y:e.clientY}; };
        window.addEventListener('pointermove', window.__swPointerObserver, true);
    }''')
    try:
        box = await composer.bounding_box()
        if not box or box['width'] < 12 or box['height'] < 12:
            raise PointerPreparationError('composer_not_visible')
        metrics = await page.evaluate('''() => ({outerWidth,outerHeight,innerWidth,innerHeight,
            screenWidth:screen.width,screenHeight:screen.height})''')
        frame = await driver.window_geometry(target)
        if (metrics['innerWidth'] <= 0 or metrics['innerHeight'] <= 0 or
            metrics['outerWidth'] < metrics['innerWidth'] or metrics['outerHeight'] < metrics['innerHeight']):
            raise PointerPreparationError('window_geometry_unavailable')
        x = box['x'] + box['width'] * rng.uniform(.35, .65)
        y = box['y'] + box['height'] * rng.uniform(.35, .65)
        if not (0 <= x < metrics['innerWidth'] and 0 <= y < metrics['innerHeight']):
            raise PointerPreparationError('composer_outside_viewport')
        # Starting from a clamped screen corner avoids assuming the position
        # left by another application. Click is impossible until a real pointer
        # event is observed over this exact editor.
        # Chrome on GNOME can include shadow/decorations in outerWidth and
        # outerHeight, while Mutter reports the visible frame without them.
        if (frame['width'] < metrics['innerWidth'] or frame['height'] < metrics['innerHeight'] or
            abs(frame['width']-metrics['outerWidth']) > 100 or abs(frame['height']-metrics['outerHeight']) > 100):
            raise PointerPreparationError('window_geometry_mismatch')
        sx = frame['x'] + (frame['width'] - metrics['innerWidth']) / 2 + x
        sy = frame['y'] + frame['height'] - metrics['innerHeight'] + y
        width,height=metrics['screenWidth'],metrics['screenHeight']
        if not (2 <= sx < width-2 and 2 <= sy < height-2):
            raise PointerPreparationError('unsupported_display_geometry')
        start = time.monotonic()
        # Bottom-right avoids GNOME's top-left Activities hot corner.
        await pointer.move(100000, 100000)
        previous_x, previous_y = width - 1, height - 1
        for step, (next_x, next_y) in enumerate(focus_path(sx, sy, width, height, rng), 1):
            # The helper validates the reserved account/window for every move.
            await pointer.move(next_x - previous_x, next_y - previous_y)
            previous_x, previous_y = next_x, next_y
            remaining = start + FOCUS_MOTION_SECONDS * step / FOCUS_MOTION_STEPS - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
        motion_elapsed = time.monotonic() - start
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        over_input = await composer.evaluate('''el => {
            const p = window.__swPointerLast;
            const hit = p && document.elementFromPoint(p.x,p.y);
            return !!hit && (el === hit || el.contains(hit));
        }''')
        if not over_input:
            raise PointerPreparationError('pointer_not_over_input')
        # Both directions, one notch each; preserve the original viewport position.
        direction = 1 if rng.randrange(2) else -1
        await pointer.scroll(direction)
        await asyncio.sleep(.06)
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        await pointer.scroll(-direction)
        await asyncio.sleep(.06)
        record_input_action(target, 'move_and_paired_scroll', motion_seconds=FOCUS_MOTION_SECONDS,
                            motion_elapsed_seconds=round(motion_elapsed,4),
                            steps=FOCUS_MOTION_STEPS, scroll=[direction,-direction])
        # Scrolling can move the page, so confirm the hit and focus again.
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        if not await composer.evaluate('''el => {
            const p=window.__swPointerLast, hit=p && document.elementFromPoint(p.x,p.y);
            return !!hit && (el===hit || el.contains(hit));
        }'''):
            raise PointerPreparationError('input_moved_after_scroll')
        for click_index in range(1,FOCUS_CLICKS+1):
            # Confirm the same input again before each native click. Never click Send here.
            if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
                raise PointerPreparationError('window_focus_lost')
            if not await composer.evaluate('''el => {
                const p=window.__swPointerLast, hit=p && document.elementFromPoint(p.x,p.y);
                return !!hit && (el===hit || el.contains(hit));
            }'''):
                raise PointerPreparationError('input_moved_before_click')
            await pointer.click()
            if not await driver.confirm_focus(target, page, composer):
                raise PointerPreparationError('input_focus_unconfirmed')
            record_input_action(target, 'composer_clicked_and_focused',click_index=click_index,clicks=FOCUS_CLICKS)
            if click_index<FOCUS_CLICKS:
                await asyncio.sleep(.12)
    finally:
        await page.evaluate('''() => {
            if (window.__swPointerObserver) window.removeEventListener('pointermove',window.__swPointerObserver,true);
            delete window.__swPointerObserver; delete window.__swPointerLast;
        }''')
