"""Build auditable audio provenance for library archive."""
import time

def music_metadata(clip, title, prompt, source_id, clip_id, email):
    manual=clip.get('tags',[]);auto=clip.get('auto_tags',[])
    for values in (manual,auto):
        if not isinstance(values,list) or len(values)>30 or any(not isinstance(v,str) or not v.strip() or len(v)>80 for v in values):
            raise ValueError('音乐标签格式不正确。')
    details={'title':title,'generation_prompt':prompt,'manual_tags':manual,
             'auto_tags':auto,'tag_source':'prompt_intent','audio_analyzed':False}
    if 'style_id' in clip:
        v=clip['style_id']
        if not isinstance(v,str) or not v.strip() or len(v)>256:raise ValueError('画风 ID 不正确。')
        details['style_id']=v
    return {'source':'flow_music','source_id':source_id,'media_type':'audio',
            'prompt':prompt,'account_email':email,'media_id':clip_id or source_id,
            'request_id':clip.get('request_id',''),'model':clip.get('model') or 'flow-music',
            'created':time.time(),'details':details,'tags':manual,
            'structured_tags':{'keywords':auto},'tag_source':'music_prompt',
            'index_provenance':{'source':'music_prompt','meaning':'prompt_intent','audio_analyzed':False},
            'index_status':'ready'}
