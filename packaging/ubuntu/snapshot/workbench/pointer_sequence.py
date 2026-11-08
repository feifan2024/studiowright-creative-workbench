"""Fail-closed desktop pointer preparation; caller owns the foreground queue."""
import asyncio
import random
import time
import json
import os
from pathlib import Path

INPUT_SEQUENCE_VERSION = "observed-pointer-correction-20261108"

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


async def confirm_pointer_over_input(*, composer, target, driver, pointer):
    """Wait for native motion delivery and correct only from trusted observations."""
    corrections = 0
    for attempt in range(7):
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        observation = await composer.evaluate('''el => {
            const p=window.__swPointerLast, r=el.getBoundingClientRect();
            const hit=p && document.elementFromPoint(p.x,p.y);
            return {hit:!!hit && (el===hit || el.contains(hit)),
                x:p && p.x, y:p && p.y,
                centerX:r.x+r.width/2, centerY:r.y+r.height/2,
                visible:r.width>=12 && r.height>=12 && r.x>=0 && r.y>=0 &&
                    r.right<=innerWidth && r.bottom<=innerHeight};
        }''')
        if observation.get('hit'):
            return corrections
        # Give the compositor/browser time to deliver the last native event.
        # A missing observation never authorizes a guessed click.
        if attempt >= 2 and observation.get('visible') and corrections < 3:
            x, y = observation.get('x'), observation.get('y')
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                dx = max(-200, min(200, observation['centerX'] - x))
                dy = max(-200, min(200, observation['centerY'] - y))
                await pointer.move(dx, dy)
                corrections += 1
        await asyncio.sleep(.05)
    raise PointerPreparationError('pointer_not_over_input')


async def wait_for_input_layout(*, composer, target, driver):
    """Do not calculate desktop coordinates during window/layout transitions."""
    await composer.scroll_into_view_if_needed(timeout=3000)
    previous = None
    for _ in range(12):
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        box = await composer.bounding_box()
        frame = await driver.window_geometry(target)
        if box and box['width'] >= 12 and box['height'] >= 12:
            current = tuple(round(box[k], 1) for k in ('x', 'y', 'width', 'height')) + tuple(
                frame[k] for k in ('x', 'y', 'width', 'height'))
            if current == previous:
                return
            previous = current
        else:
            previous = None
        await asyncio.sleep(.05)
    raise PointerPreparationError('input_layout_unstable')


async def prepare_pointer(*, page, composer, target, driver, pointer, rng=None):
    """Move the OS pointer, scroll, click the input, and confirm focus.

    The provider is never submitted from here. Pointer input is supplied by an
    already authorised desktop session. A DOM mouse observation guards against
    inaccurate window decorations, display scale, or multiple monitors.
    """
    rng = rng or random.SystemRandom()
    if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
        raise PointerPreparationError('window_focus_unconfirmed')
    await wait_for_input_layout(composer=composer, target=target, driver=driver)
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
        corrections = await confirm_pointer_over_input(composer=composer, target=target,
            driver=driver, pointer=pointer)
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
                            steps=FOCUS_MOTION_STEPS, scroll=[direction,-direction],
                            corrections=corrections)
        # Scrolling can move the page, so confirm the hit and focus again.
        if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
            raise PointerPreparationError('window_focus_lost')
        await confirm_pointer_over_input(composer=composer, target=target,
            driver=driver, pointer=pointer)
        for click_index in range(1,FOCUS_CLICKS+1):
            # Confirm the same input again before each native click. Never click Send here.
            if not await driver._identity_confirmed(target) or not await driver._os_focus_confirmed(target):
                raise PointerPreparationError('window_focus_lost')
            await confirm_pointer_over_input(composer=composer, target=target,
                driver=driver, pointer=pointer)
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
