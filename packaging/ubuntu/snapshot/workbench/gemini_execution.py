"""Shared, configurable deadlines for one provider call; progress is actual text."""
import json
import math
import time

DEFAULTS = {
    'text_total_seconds': 600,
    'image_total_seconds': 600,
    'idle_seconds': 240,
    'probe_total_seconds': 180,
    'probe_idle_seconds': 120,
}


def execution_limits(runtime):
    limits = dict(DEFAULTS)
    path = runtime.config.parent / 'gemini-execution.json'
    if not path.exists():
        return limits
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('invalid_gemini_execution_limits')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.keys() - limits.keys():
        raise ValueError('invalid_gemini_execution_limits')
    for key, value in data.items():
        if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 3600:
            raise ValueError('invalid_gemini_execution_limits')
    limits.update(data)
    return limits


class CallBudget:
    def __init__(self, total_seconds, idle_seconds, clock=time.monotonic):
        self.total_seconds = total_seconds
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.started = self.last_progress = clock()

    def progress(self):
        self.last_progress = self.clock()

    def expired(self):
        now = self.clock()
        if now - self.started >= self.total_seconds:
            return 'total'
        if now - self.last_progress >= self.idle_seconds:
            return 'idle'
        return None


def call_budget(runtime, kind):
    limits = execution_limits(runtime)
    if kind == 'probe':
        return CallBudget(limits['probe_total_seconds'], limits['probe_idle_seconds'])
    return CallBudget(limits[kind + '_total_seconds'], limits['idle_seconds'])
