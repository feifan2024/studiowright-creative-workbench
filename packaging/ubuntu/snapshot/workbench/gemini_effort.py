"""Verified local CLI model capabilities for the configured Flash family."""
MODELS={level:'gemini-3.8-flash-'+level for level in ('low','medium','high')}
DEFAULT=MODELS['medium']
def resolve(model=None,effort=None):
    if effort is not None and (not isinstance(effort,str) or effort not in MODELS):
        return DEFAULT
    if model not in (None,'',DEFAULT,*MODELS.values()):raise ValueError('当前仅支持 Gemini 3.8 Flash。')
    if effort and model not in (None,'',DEFAULT,MODELS[effort]):raise ValueError('model 与 effort 不一致。')
    return MODELS[effort] if effort else model or DEFAULT
