from __future__ import annotations
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jyexport.codec import Codec, CodecError, _string, _value
from jyexport.discovery import Draft, scan
from jyexport.draft import VARIANTS, assert_classified, inspect, srt, timestamp, transform
from jyexport.storage import create_session, prepare_job, register_prepared, safe_name, sha, snapshot, validate_session
from jyexport.media import archive_srt, archive_video, probe

def sample():
    def segment(sid,mid,start=0,duration=1_000_000):
        return {'id':sid,'material_id':mid,'volume':.7,'target_timerange':{'start':start,'duration':duration},'source_timerange':{'start':999,'duration':duration},'extra_material_refs':['fade1']}
    return {'id':'original','duration':2_000_000,'fps':30,'canvas_config':{'width':1080,'height':1920},
        'tracks':[
            {'id':'v','type':'video','segments':[segment('v1','vmat',0,2_000_000)]},
            {'id':'a','type':'audio','name':'混排轨道','segments':[segment('voice','voice'),segment('bgm','music')]},
            {'id':'b','type':'audio','name':'另一条配乐','segments':[segment('bgm2','music'),segment('sfx','sound'),segment('amb','amb')]},
            {'id':'t','type':'text','segments':[segment('sub1','sub',100000,900000),segment('sub2','sub',1000000,1000000),segment('title','title')]},
        ],'materials':{'videos':[{'id':'vmat','type':'video'}],
            'audios':[{'id':'voice','type':'extract_music'},{'id':'music','type':'music'},{'id':'sound','type':'sound'},{'id':'amb','type':'audio'}],
            'texts':[{'id':'sub','type':'subtitle','content':json.dumps({'text':'Hola\\nmundo'})},{'id':'title','type':'text','content':json.dumps({'text':'片名'})}],
            'audio_fades':[{'id':'fade1','fade_in_duration':50000}]}}

def roles():
    return {'a:voice':'人声','a:bgm':'背景音乐','b:bgm2':'背景音乐','b:sfx':'音效','b:amb':'环境音','t:sub1':'字幕','t:sub2':'字幕','t:title':'标题/其他文字'}

class DraftTests(unittest.TestCase):
    def test_four_variants_and_original_immutable(self):
        data=sample();before=copy.deepcopy(data)
        for key,setting in VARIANTS.items():
            changed=transform(data,roles(),key)
            segments=[s['id'] for t in changed['tracks'] for s in t['segments']]
            self.assertEqual('bgm' in segments,setting['bgm'])
            self.assertEqual('bgm2' in segments,setting['bgm'])
            self.assertEqual('sub1' in segments,setting['subtitles'])
            for sid in ('v1','voice','sfx','amb','title'):self.assertIn(sid,segments)
            self.assertEqual(changed['tracks'][0],data['tracks'][0])
            self.assertEqual(changed['materials'],data['materials'])
            self.assertEqual(changed['fps'],60)
            self.assertEqual(changed['canvas_config']['height'],1920)
        self.assertEqual(data,before)

    def test_unknown_audio_blocks_transform(self):
        classified=roles();del classified['b:amb']
        with self.assertRaises(ValueError):transform(sample(),classified,'V1')

    def test_wrong_category_blocks(self):
        classified=roles();classified['t:title']='背景音乐'
        with self.assertRaises(ValueError):assert_classified(sample(),classified)

    def test_srt_uses_target_not_source_and_preserves_newline(self):
        result=srt(sample(),roles())
        self.assertIn('00:00:00,100 --> 00:00:01,000\nHola\nmundo',result)
        self.assertIn('00:00:01,000 --> 00:00:02,000',result)
        self.assertNotIn('片名',result)
        self.assertEqual(result.count(' --> '),2)

    def test_timestamp_carries_and_rounds(self):
        self.assertEqual(timestamp(3_599_999_600),'01:00:00,000')
        self.assertEqual(timestamp(-999),'00:00:00,000')
        self.assertEqual(timestamp(36_000_000_000),'10:00:00,000')

    def test_compound_stops_transforms(self):
        data=sample();data['materials']['videos'].append({'id':'compound','type':'draft'})
        with self.assertRaises(ValueError):transform(data,roles(),'V1')
        with self.assertRaises(ValueError):srt(data,roles())

    def test_audio_suggestions_are_not_accepted_implicitly(self):
        rows=inspect(sample())
        self.assertEqual(next(r for r in rows if r['key']=='a:voice')['suggestion'],'待确认')
        self.assertEqual(next(r for r in rows if r['key']=='b:sfx')['suggestion'],'音效')

    def test_msvc_long_input_keeps_memory_alive(self):
        value,keepalive=_string('中文'*5000)
        self.assertIsNotNone(keepalive)
        self.assertEqual(_value(value),'中文'*5000)

    def test_msvc_inline(self):
        value,keepalive=_string('hola')
        self.assertEqual(_value(value),'hola')

class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jyexport_test_');self.base=Path(self.temp.name)
        self.source=self.base/'drafts/剧名';self.source.mkdir(parents=True)
        self.content=self.source/'draft_content.json';self.content.write_text(json.dumps(sample(),ensure_ascii=False),'utf-8')
        (self.source/'Resources').mkdir();(self.source/'Resources/file.bin').write_bytes(b'asset')
        self.draft=Draft('draftkey','剧名',str(self.source),str(self.content),'','第1集',False,2_000_000,1)

    def tearDown(self):self.temp.cleanup()

    def make_session(self,variants=None,want_srt=True):
        return create_session([self.draft],self.base/'output',list(VARIANTS) if variants is None else variants,want_srt)

    def test_snapshot_hashes_and_original_unchanged(self):
        before=sha(self.content);result=snapshot(self.source,self.base/'backup')
        self.assertEqual(len(result['files']),2)
        self.assertEqual(sha(self.content),before)
        self.assertEqual(sha(self.base/'backup/draft_content.json'),before)

    def test_no_backup_inside_source(self):
        with self.assertRaises(ValueError):snapshot(self.source,self.source/'backup')
        with self.assertRaises(ValueError):create_session([self.draft],self.source/'out',['V1'])

    def test_snapshot_detects_concurrent_change(self):
        import jyexport.storage as storage
        real_copy=storage.shutil.copy2
        def changing(source,dest):
            result=real_copy(source,dest)
            if Path(source)==self.content:self.content.write_text('{"changed":true}','utf-8')
            return result
        with patch('jyexport.storage.shutil.copy2',side_effect=changing):
            with self.assertRaises(RuntimeError):snapshot(self.source,self.base/'backup')

    def test_scan_excludes_cache_recycle_and_deleted_timeline(self):
        for directory in ('.recycle_bin','.cloud_cache_test'):
            p=self.base/'drafts'/directory/'draft';p.mkdir(parents=True);(p/'draft_content.json').write_text('{}')
        timeline=self.source/'Timelines/one';timeline.mkdir(parents=True);(timeline/'draft_content.json').write_text('{}')
        (self.source/'Timelines/project.json').write_text(json.dumps({'timelines':[{'id':'one','name':'第1集'},{'id':'deleted','is_marked_delete':True},{'id':'missing','name':'缺失集'}]}),'utf-8')
        found,warnings=scan([self.base/'drafts'])
        self.assertEqual(len(found),1);self.assertEqual(found[0].timeline_id,'one');self.assertEqual(len(warnings),1)

    def test_lifecycle_prepares_four_distinct_clones_without_changing_source(self):
        before=sha(self.content);folder,session=self.make_session();job=session['jobs'][0]
        job['roles']=roles();job['roles_content_sha256']=sha(job['backup_content'])
        prepare_job(folder,session,job,Codec())
        self.assertEqual(len(job['prepared']),4)
        ids=[]
        for variant,item in job['prepared'].items():
            data=json.loads((Path(item['folder'])/'draft_content.json').read_text('utf-8'))
            ids.append(data['id']);self.assertNotEqual(data['id'],'original')
            self.assertFalse((Path(item['folder'])/'Timelines').exists())
            self.assertEqual(data['fps'],60)
        self.assertEqual(len(set(ids)),4);self.assertEqual(before,sha(self.content))
        self.assertTrue(Path(job['srt']['file']).is_file())
        self.assertTrue(all(item['file'] is None for item in job['variants'].values()))

    def test_tampered_backup_blocks_preparation(self):
        folder,session=self.make_session();job=session['jobs'][0]
        Path(job['backup_content']).write_text('{}','utf-8')
        with self.assertRaises(ValueError):prepare_job(folder,session,job,Codec())

    def test_srt_only_does_not_require_audio_roles(self):
        folder,session=self.make_session([]);job=session['jobs'][0]
        job['roles']={k:v for k,v in roles().items() if k.startswith('t:')};job['roles_content_sha256']=sha(job['backup_content'])
        prepare_job(folder,session,job,Codec())
        self.assertTrue(Path(job['srt']['file']).exists());self.assertFalse(job['prepared'])

    def test_manifest_path_escape_rejected(self):
        folder,session=self.make_session();session['jobs'][0]['directory']=str(self.source)
        with self.assertRaises(ValueError):validate_session(folder,session)

    def test_stale_classification_rejected(self):
        folder,session=self.make_session();job=session['jobs'][0];job['roles']=roles();job['roles_content_sha256']='old'
        with self.assertRaises(ValueError):prepare_job(folder,session,job,Codec())

    def test_srt_import_validation_and_no_overwrite(self):
        folder,session=self.make_session();job=session['jobs'][0]
        source=self.base/'source.srt';source.write_text('1\n00:00:00,000 --> 00:00:01,000\n你好\n','utf-8')
        archive_srt(folder,session,job,source);self.assertEqual(job['srt']['status'],'待审核')
        with self.assertRaises(FileExistsError):archive_srt(folder,session,job,source)
        source.write_text('1\n00:00:03,000 --> 00:00:01,000\n错误','utf-8')
        with self.assertRaises(ValueError):archive_srt(folder,session,job,source)

    def test_register_requires_editor_closed_and_backs_up_index(self):
        folder,session=self.make_session();job=session['jobs'][0];job['roles']=roles();job['roles_content_sha256']=sha(job['backup_content'])
        prepare_job(folder,session,job,Codec())
        fake_user=self.base/'user';index=fake_user/'Projects/com.lveditor.draft/root_meta_info.json';index.parent.mkdir(parents=True)
        original={'all_draft_store':[{'draft_id':'old'}],'draft_ids':['old'],'root_path':'test'};index.write_text(json.dumps(original),'utf-8')
        running=subprocess.CompletedProcess([],0,stdout=b'"JianyingPro.exe"')
        stopped=subprocess.CompletedProcess([],0,stdout=b'other.exe')
        with patch('jyexport.storage.user_data',return_value=fake_user):
            with patch('jyexport.storage.subprocess.run',return_value=running):
                with self.assertRaises(RuntimeError):register_prepared(folder,session)
            self.assertEqual(json.loads(index.read_text('utf-8')),original)
            with patch('jyexport.storage.subprocess.run',return_value=stopped):self.assertEqual(register_prepared(folder,session),4)
        self.assertEqual(len(json.loads(index.read_text('utf-8'))['all_draft_store']),5)
        backups=list(folder.glob('草稿列表备份_*.json'));self.assertEqual(len(backups),1);self.assertEqual(json.loads(backups[0].read_text('utf-8')),original)

    def test_safe_names(self):
        self.assertEqual(safe_name('CON.txt'),'_CON.txt');self.assertNotIn('/',safe_name('../危险/name'))

class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.appdir=Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')
        if not (cls.appdir/'ffmpeg.exe').exists():raise unittest.SkipTest('需要设置 JY_TEST_APP_DIR 以使用本机剪映FFmpeg')
        cls.temp=tempfile.TemporaryDirectory(prefix='jyexport_media_test_');cls.base=Path(cls.temp.name)
        for fps,name in [(60,'valid'),(30,'invalid')]:
            result=subprocess.run([str(cls.appdir/'ffmpeg.exe'),'-nostdin','-y','-v','error','-f','lavfi','-i',f'color=c=black:s=1080x1920:r={fps}',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','0.5','-c:v','mpeg4','-q:v','12','-c:a','aac',str(cls.base/(name+'.mp4'))],capture_output=True,timeout=60)
            if result.returncode:raise RuntimeError(result.stderr.decode('utf-8',errors='replace'))

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'temp'):cls.temp.cleanup()

    def test_real_1080p60_probe_and_full_decode(self):
        result=probe(self.base/'valid.mp4',self.appdir,500000,decode=True)
        self.assertTrue(result['full_decode']);self.assertEqual(result['fps'],'60')

    def test_wrong_fps_rejected(self):
        with self.assertRaises(ValueError):probe(self.base/'invalid.mp4',self.appdir)

    def test_wrong_duration_rejected(self):
        with self.assertRaises(ValueError):probe(self.base/'valid.mp4',self.appdir,10000000)

    def test_archive_does_not_claim_manual_review(self):
        source=self.base/'fixture';source.mkdir(exist_ok=True);content=source/'draft_content.json';content.write_text('{}')
        draft=Draft('k','测试',str(source),str(content),'','集1',False,500000,0)
        folder,session=create_session([draft],self.base/'sessions',['V4'],False);job=session['jobs'][0]
        archive_video(folder,session,job,'V4',self.base/'valid.mp4',self.appdir)
        self.assertFalse(job['variants']['V4']['reviewed']);self.assertEqual(session['status'],'待审核')
        with self.assertRaises(FileExistsError):archive_video(folder,session,job,'V4',self.base/'valid.mp4',self.appdir)

if __name__=='__main__':unittest.main(verbosity=2)
