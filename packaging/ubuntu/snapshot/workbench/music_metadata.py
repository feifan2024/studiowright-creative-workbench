"""Local prompt-intent tags; never claims audio was analyzed or calls a model."""
import re

RULES = {
    '背景音乐': r'背景音乐|配乐|\bbgm\b|background music|underscore',
    '音效': r'音效|\bsfx\b|sound effects?',
    '钢琴': r'钢琴|\bpiano\b', '吉他': r'吉他|\bguitar\b',
    '弦乐': r'弦乐|\bstrings\b', '鼓点': r'鼓点|\bdrums?\b',
    '电子': r'电子|\belectronic\b|\bedm\b', '古风': r'古风|国风',
    '爵士': r'爵士|\bjazz\b', '摇滚': r'摇滚|\brock\b',
    '轻快': r'轻快|欢快|\bupbeat\b|cheerful', '舒缓': r'舒缓|柔和|\bcalm\b|gentle|relaxing',
    '紧张': r'紧张|\btense\b|tension', '悬疑': r'悬疑|suspense',
    '悲伤': r'悲伤|\bsad\b|melancholy', '史诗': r'史诗|\bepic\b',
    '口播': r'口播|解说|voiceover|narration', '打斗': r'打斗|战斗|combat|battle',
    '片头': r'片头|opening theme|\bintro\b', '短视频': r'短视频|short video',
    '环境音': r'环境音|自然声|ambient sounds?', '雨声': r'雨声|rain sounds?',
    '鸟鸣': r'鸟鸣|birdsong|bird calls?',
    '纯音乐': r'纯音乐|无人声|无歌词|不要人声|不需要人声|没有人声|不含人声|\binstrumental\b|no vocals?|without vocals?',
    '男声': r'男声|\bmale vocals?\b', '女声': r'女声|\bfemale vocals?\b',
}

def validate_metadata(value):
    if not isinstance(value, dict) or set(value)-{'tags','style_id'}:
        raise ValueError('音乐标签参数不正确。')
    out = {}
    if 'tags' in value:
        tags=value['tags']
        if not isinstance(tags,list) or len(tags)>30 or any(not isinstance(t,str) or not t.strip() or len(t)>80 or any(0xD800<=ord(c)<=0xDFFF for c in t) for t in tags):
            raise ValueError('手动标签最多30个，每个1～80字。')
        out['tags']=list(tags)
    if 'style_id' in value:
        v=value['style_id']
        if not isinstance(v,str) or not v.strip() or len(v)>256 or any(0xD800<=ord(c)<=0xDFFF for c in v):
            raise ValueError('画风 ID 必须是1～256字的非空字符串。')
        out['style_id']=v
    return out

def prompt_tags(prompt):
    result=[]
    for label,pattern in RULES.items():
        for m in re.finditer(pattern,prompt,re.I):
            prefix=prompt[max(0,m.start()-16):m.start()]
            if re.search(r'(?:不要|不需要|避免|不含|没有|不是|非|无)\s*[^，。；;,.]{0,3}$|\b(?:no|not|without|avoid)\s+$',prefix,re.I):
                continue
            result.append(label);break
    return result

def attach_metadata(result,prompt,model,request_id,metadata):
    metadata=validate_metadata(metadata)
    auto=prompt_tags(prompt)
    return {**result, **metadata, 'auto_tags':auto, 'tag_source':'prompt_intent',
            'clips':[{**clip,**metadata,'auto_tags':auto,'tag_source':'prompt_intent',
                      'prompt':prompt,'model':model,'request_id':request_id}
                     for clip in result['clips']]}
