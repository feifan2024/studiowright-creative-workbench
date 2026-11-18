import json
import sys
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import Mock, patch
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'packaging/ubuntu/snapshot/flow/console'))
sys.path.insert(0,str(root/'packaging/ubuntu/snapshot/workbench'))
from music_jobs import MusicJobs
from music_metadata import prompt_tags, validate_metadata, attach_metadata
from music_import import music_metadata
from material_library import Library
from unified_api import allowed_route
from test_flow_regressions import fixture_directory

class MusicMetadata(unittest.TestCase):
    def test_prompt_intent_and_negation(self):
        tags=prompt_tags('轻快的钢琴背景音乐，无人声，适合口播，不要吉他，不要鼓点。')
        self.assertTrue({'轻快','钢琴','背景音乐','纯音乐','口播'}<=set(tags))
        self.assertNotIn('吉他',tags);self.assertNotIn('鼓点',tags)
        self.assertNotIn('男声',prompt_tags('female vocals, no piano'))
        self.assertNotIn('钢琴',prompt_tags('female vocals, no piano'))
        self.assertEqual(prompt_tags('unclassified experimental description'),[])

    def test_manual_values_are_exact_and_invalid_rejected(self):
        m={'tags':['  标签 A  ','标签 A','标签 A'],'style_id':'  Style/001  '}
        self.assertEqual(validate_metadata(m),m)
        for invalid in [{'tags':'a'},{'tags':[None]},{'tags':['a']*31},{'style_id':None},{'style_id':''}]:
            with self.assertRaises(ValueError):validate_metadata(invalid)

    def test_job_recovery_retains_metadata_and_never_regenerates(self):
        with fixture_directory() as d:
            gen=Mock(return_value={'clips':[{'title':'fixture'}]})
            ingest=Mock(return_value={'ingested':[{'ok':True,'asset_id':'a'*64,'path':'/api/library/'+('a'*64)+'/file'}]})
            jobs=MusicJobs(d,gen,ingest);ident=str(uuid.uuid4());m={'tags':[' 原样 '],'style_id':' S/1 '}
            with patch('music_jobs.threading.Thread'):
                jobs.submit(ident,'轻快钢琴','lyria-fast',m)
            jobs.run(ident,'轻快钢琴','lyria-fast')
            row=jobs.get(ident);self.assertEqual(row['tags'],m['tags']);self.assertEqual(row['result']['clips'][0]['style_id'],m['style_id'])
            self.assertEqual(row['result']['library'][0]['asset_id'],'a'*64)
            self.assertEqual(jobs.submit(ident,'轻快钢琴','lyria-fast',m)['id'],ident)
            with self.assertRaises(ValueError):jobs.submit(ident,'轻快钢琴','lyria-fast',{'tags':['changed']})
            jobs.save(ident,'ingesting',row['result'])
            resumed=MusicJobs(d,Mock(),ingest);resumed.recover=Mock();resumed.reconcile()
            self.assertEqual(resumed.get(ident)['status'],'completed')
            self.assertEqual(ingest.call_args.args[0][0]['tags'],[' 原样 '])
            resumed.generate.assert_not_called();resumed.recover.assert_not_called();gen.assert_called_once()

    def test_library_archive_filter_download_and_manual_edit_preserved(self):
        with fixture_directory() as d:
            audio=d/'a.wav'
            with wave.open(str(audio),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*800)
            library=Library(d/'library');m={'tags':[' 手工标签 '],'style_id':' Style-A '}
            clip=attach_metadata({'clips':[{}]},'轻快钢琴背景音乐','lyria-fast','request',m)['clips'][0]
            meta=music_metadata(clip,'title',clip['prompt'],'job/clip/audio','clip','')
            ident=library.archive(audio,meta);item=library.get(ident)
            self.assertAlmostEqual(item['duration'],0.1)
            self.assertEqual(item['audio_category'],'music')
            with library.connect() as db:
                db.execute('UPDATE assets SET duration=NULL WHERE id=?',(ident,))
            self.assertEqual(library.backfill_audio_durations(),{'scanned':1,'updated':1})
            self.assertAlmostEqual(library.get(ident)['duration'],0.1)
            self.assertEqual(library.backfill_audio_durations(),{'scanned':0,'updated':0})
            self.assertEqual(item['origins'][0]['style_id'],' Style-A ')
            self.assertEqual(item['origins'][0]['details']['manual_tags'],[' 手工标签 '])
            self.assertEqual(item['tags'],[' 手工标签 ']);self.assertEqual(library.file(ident).read_bytes(),audio.read_bytes())
            for tag in ['钢琴',' 手工标签 ']:self.assertEqual(library.search(kind='audio',tag=tag,style_id=' Style-A ')['total'],1)
            self.assertEqual(library.search(style_id='Style-A')['total'],0)
            self.assertEqual(library.archive(audio,meta),ident)
            library.set_dimensions(ident,{'music_instrument':['人工改过']});library.archive(audio,meta)
            self.assertEqual(library.get(ident)['structured_tags']['music_instrument'],['人工改过'])
            self.assertEqual(library.search()['total'],1)

    def test_audio_category_uses_explicit_source_or_other(self):
        with fixture_directory() as d:
            audio=d/'effect.wav'
            with wave.open(str(audio),'wb') as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b'\0\0'*400)
            library=Library(d/'library')
            ident=library.archive(audio,{'source':'import','source_id':'generic'})
            self.assertEqual(library.get(ident)['audio_category'],'other')
            self.assertEqual(library.archive(audio,{'source':'import','source_id':'effect',
                                                    'details':{'audio_category':'sound_effect'}}),ident)
            self.assertEqual(library.search(kind='audio')['items'][0]['audio_category'],'sound_effect')

    def test_authenticated_gateway_forwards_job_metadata_and_duplicate_once(self):
        import threading
        import time
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, build_opener, ProxyHandler
        from urllib.error import HTTPError
        from server import make_handler
        from unified_api import handler_for
        from unified_keys import UnifiedKeys
        with fixture_directory() as d:
            pool=Mock();pool.state_dir=d;pool.path=d/'unused.db';pool.maintenance.return_value=False
            generated=Mock(return_value={'clips':[{'title':'test'}]})
            ingested=Mock(return_value={'ingested':[{'ok':True,'asset_id':'a'*64,'path':'/api/library/'+('a'*64)+'/file'}]})
            with patch('server.music_generate',generated),patch('server.music_library_ingest',ingested),patch('music_recovery.recover',return_value=None):
                sources=Mock()
                work=ThreadingHTTPServer(('127.0.0.1',0),make_handler(sources,real_pool=pool))
                store=UnifiedKeys(d/'keys.db');key=store.create('fixture')['key']
                api=ThreadingHTTPServer(('127.0.0.1',0),handler_for(store,work.server_port))
                threads=[threading.Thread(target=x.serve_forever,daemon=True) for x in [work,api]]
                for t in threads:t.start()
                op=build_opener(ProxyHandler({}));base='http://127.0.0.1:'+str(api.server_port)
                def call(path,data=None,auth=True):
                    headers={'Content-Type':'application/json'}
                    if auth:headers['Authorization']='Bearer '+key
                    with op.open(Request(base+path,headers=headers,data=json.dumps(data).encode() if data is not None else None),timeout=5) as r:return json.load(r)
                try:
                    rid=str(uuid.uuid4());body={'request_id':rid,'prompt':'轻快钢琴','model':'lyria-fast','tags':[' Exact '],'style_id':' S-1 '}
                    with self.assertRaises(HTTPError) as error:call('/api/music/jobs',body,False)
                    self.assertEqual(error.exception.code,401)
                    call('/api/music/jobs',body);call('/api/music/jobs',body)
                    for _ in range(100):
                        job=call('/api/music/jobs/'+rid)['job']
                        if job['status']=='completed':break
                        time.sleep(.02)
                    self.assertEqual(job['status'],'completed')
                    self.assertEqual(job['style_id'],' S-1 ')
                    self.assertEqual(job['result']['clips'][0]['tags'],[' Exact '])
                    generated.assert_called_once()
                    import io
                    from email.message import Message
                    def media(*args,**kwargs):
                        partial=kwargs.get('range_header') is not None
                        res=io.BytesIO(b'RIFF' if partial else b'RIFFfixtureWAVE')
                        res.status=206 if partial else 200;res.headers=Message()
                        res.headers['Content-Type']='audio/wav';res.headers['Content-Length']=str(len(res.getvalue()))
                        res.headers['Accept-Ranges']='bytes'
                        if partial:res.headers['Content-Range']='bytes 0-3/15'
                        return res
                    sources.library.side_effect=media
                    for partial in [False,True]:
                        headers={'Authorization':'Bearer '+key}
                        if partial:headers['Range']='bytes=0-3'
                        with op.open(Request(base+'/api/library/'+('a'*64)+'/file',headers=headers),timeout=5) as response:
                            self.assertEqual(response.headers.get_content_type(),'audio/wav')
                            self.assertEqual(response.status,206 if partial else 200)
                            self.assertEqual(response.read(),b'RIFF' if partial else b'RIFFfixtureWAVE')
                    with self.assertRaises(HTTPError) as error:call('/api/music/jobs',{**body,'tags':['changed']})
                    self.assertEqual(error.exception.code,400)
                    with self.assertRaises(HTTPError):call('/api/music/readiness',{})
                finally:
                    for x in [api,work]:x.shutdown();x.server_close()
                    for t in threads:t.join()

    def test_gateway_only_exposes_public_music_job_routes(self):
        self.assertEqual(allowed_route('POST','/api/music/jobs'),'workbench')
        self.assertEqual(allowed_route('GET','/api/music/jobs/'+str(uuid.uuid4())),'workbench')
        for route in ['/api/music/jobs/latest','/api/music/generate','/api/music/readiness','/api/tokens']:
            self.assertIsNone(allowed_route('POST',route));self.assertIsNone(allowed_route('GET',route))
        self.assertIsNone(allowed_route('POST','/api/music/jobs?other=1'))

if __name__=='__main__':unittest.main()
