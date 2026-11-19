"""Resolve the model identifiers accepted by the workbench."""
MODELS={level:'gemini-3.8-flash-'+level for level in ('low','medium','high')}
DEFAULT=MODELS['medium']
NEON='gemini-4-neon-high'
def resolve(model=None,effort=None):
    if model == NEON:
        if effort not in (None,'high'):raise ValueError('Gemini 4 Neon 当前开放 High 档。')
        return NEON
    if effort is not None and (not isinstance(effort,str) or effort not in MODELS):
        return DEFAULT
    if model not in (None,'',DEFAULT,*MODELS.values()):raise ValueError('请选择 Gemini 3.8 Flash 或 Gemini 4 Neon High。')
    if effort and model not in (None,'',DEFAULT,MODELS[effort]):raise ValueError('model 与 effort 不一致。')
    return MODELS[effort] if effort else model or DEFAULT
