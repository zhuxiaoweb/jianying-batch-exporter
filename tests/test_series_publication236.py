"""State/receipt fixtures for atomic grouped publication, interruption and reuse."""
import copy,json,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from jyexport.advanced import REVISION,delivery_plan
from jyexport.delivery_service import prepare,confirm,verified_delivery,final_folder
from jyexport.discovery import Draft
from jyexport.series_archive import publish_grouped
from jyexport.storage import atomic_json,sha,save_session
from tests.test_advanced import Reader,document


class SeriesPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        content=self.source/'draft_content.json';content.write_text(json.dumps(document(self.source)),'utf-8')
        drafts=[Draft(str(i),'同一剧',str(self.source),str(content),'episode','时间线01',False,3000000) for i in range(2)]
        self.batch,self.session=prepare(drafts,self.root/'out',Reader(self.root,self.root),archive_mode='series')
        for job in self.session['jobs']:
            a=job['easy_analysis'];keys=[r['key'] for r in a['rows'] if r['name']=='music']
            confirm(self.batch,self.session,job,keys,a['caption_keys'])
            self.seal(job)
    def tearDown(self):self.temp.cleanup()

    def seal(self,job):
        private=Path(job['directory'])/'sealed';private.mkdir()
        report={'complete':True,'directory':str(private),'variants':{}}
        # These are deliberately state-only bytes, not decodable MP4 fixtures.
        for key in job['variants']:
            file=private/(key+'.mp4');file.write_bytes((key+job['id']+' state receipt fixture').encode())
            item={'file':str(file),'sha256':sha(file),'probe':{'full_decode':True,'width':1080,'height':1920,'fps':'60','container':'MP4','duration':3.0}}
            job['variants'][key].update(copy.deepcopy(item));report['variants'][key]=item
        file=private/'字幕.srt';file.write_text('1\n00:00:00,000 --> 00:00:01,000\nFinal\n','utf-8-sig')
        job['srt']={'file':str(file),'sha256':sha(file)};report['srt']=dict(job['srt'])
        path=private/'两母版交付报告.json';atomic_json(path,report)
        job['headless']={'revision':REVISION,'complete':True,'state':'complete','source_sha256':sha(job['backup_content']),
            'plan_sha256':delivery_plan(job),'bundle_report':str(path),'bundle_report_sha256':sha(path)}
        save_session(self.batch,self.session)

    def test_two_same_named_episodes_publish_concurrently_without_copying_or_clobber(self):
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(publish_grouped,self.session['jobs']))
        project=Path(self.session['jobs'][0]['archive_layout']['project_directory'])
        for job in self.session['jobs']:
            self.assertEqual(final_folder(job),project)
            self.assertEqual(verified_delivery(job),project)
            self.assertTrue(job['headless']['grouped_published'])
        for name in ['V1_无背景音乐_无字幕','V2_无背景音乐_有字幕','V3_有背景音乐_无字幕','V4_有背景音乐_有字幕','字幕srt']:
            self.assertEqual(len(list((project/name).iterdir())),2)
        self.assertEqual(len(list(project.parent.iterdir())),1)
        save_session(self.batch,self.session)

    def test_interrupted_move_recovers_from_original_sealed_receipt(self):
        job=self.session['jobs'][0];import os
        original=os.rename;count=0;report=Path(job['headless']['bundle_report']);before=sha(report)
        def interrupted(source,target):
            nonlocal count
            count+=1
            if count==2:raise OSError('simulated publication interruption')
            return original(source,target)
        with patch('jyexport.series_archive.os.rename',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'interruption'):publish_grouped(job)
        self.assertFalse(job['headless'].get('grouped_published'));self.assertEqual(sha(report),before)
        self.assertEqual(verified_delivery(job),Path(job['archive_layout']['project_directory']))
        stamps={key:(sha(p),Path(p).stat().st_mtime_ns) for key,p in job['archive_layout']['targets'].items()}
        verified_delivery(job)
        self.assertEqual(stamps,{key:(sha(p),Path(p).stat().st_mtime_ns) for key,p in job['archive_layout']['targets'].items()})

    def test_existing_different_content_is_preserved_and_no_source_is_moved(self):
        job=self.session['jobs'][0];target=Path(job['archive_layout']['targets']['V3']);target.parent.mkdir(parents=True)
        target.write_bytes(b'external review file');before={k:sha(v['file']) for k,v in job['variants'].items()}
        with self.assertRaisesRegex(FileExistsError,'不同内容'):publish_grouped(job)
        self.assertEqual(target.read_bytes(),b'external review file')
        self.assertEqual(before,{k:sha(v['file']) for k,v in job['variants'].items()})

    def test_changed_source_report_or_plan_is_never_published(self):
        job=self.session['jobs'][0];job['headless']['bundle_report_sha256']='wrong'
        with self.assertRaisesRegex(ValueError,'报告改变'):publish_grouped(job)
        self.assertFalse(Path(job['archive_layout']['project_directory']).exists())

    def test_report_changed_during_publication_is_not_resealed_with_a_new_hash(self):
        import os
        job=self.session['jobs'][0];path=Path(job['headless']['bundle_report']);expected=job['headless']['bundle_report_sha256']
        original=os.rename
        def changed(source,target):
            result=original(source,target)
            path.write_bytes(path.read_bytes()+b' ')
            return result
        with patch('jyexport.series_archive.os.rename',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'归类时改变'):publish_grouped(job)
        self.assertEqual(job['headless']['bundle_report_sha256'],expected)
        self.assertFalse(job['headless'].get('grouped_published'))
        self.assertFalse((Path(job['directory'])/'按剧归档报告.json').exists())

    def test_missing_source_and_target_stops_with_no_partial_publication(self):
        job=self.session['jobs'][0];Path(job['variants']['V4']['file']).unlink()
        with self.assertRaises(FileNotFoundError):publish_grouped(job)
        self.assertTrue(Path(job['variants']['V1']['file']).exists())

    def test_changed_grouped_result_cannot_be_marked_complete(self):
        job=self.session['jobs'][0];publish_grouped(job)
        Path(job['variants']['V1']['file']).write_bytes(b'changed')
        with self.assertRaises(ValueError):verified_delivery(job)

    def test_verified_delivery_rechecks_audio_before_reusing_completed_batch(self):
        job=self.session['jobs'][0]
        (self.root / 'ffmpeg.exe').write_bytes(b'test placeholder')
        with patch('jyexport.audio_diagnostics.assert_audio_safe', side_effect=ValueError('母版音频疑似削波')) as check:
            with self.assertRaisesRegex(ValueError, '母版音频疑似削波'):
                verified_delivery(job, self.root)
        self.assertEqual(check.call_count, 1)

    def test_windows_extended_prefix_from_concurrent_parent_creation_is_same_path(self):
        from jyexport.archive_layout import validate_layout,resolved_path
        original=Path.resolve
        def prefix(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            return Path('\\\\?\\'+str(value)) if path.suffix=='.srt' else value
        with patch.object(Path,'resolve',prefix):
            validate_layout(self.batch,self.session['jobs'][0])
            self.assertEqual(resolved_path(self.root/'new.srt'),self.root/'new.srt')

    def test_no_caption_omission_creates_no_empty_srt(self):
        job=self.session['jobs'][0];job['want_srt']=False
        job['delivery_options'].update(no_subtitles_confirmed=True)
        from jyexport.delivery_contract import omitted_srt
        path=Path(job['headless']['bundle_report']);report=json.loads(path.read_text('utf-8'))
        report['srt']=None;report['omissions']={'srt':omitted_srt(job)};atomic_json(path,report)
        job['headless'].update(bundle_report_sha256=sha(path),plan_sha256=delivery_plan(job))
        job['srt']={'file':None,'omitted':True}
        publish_grouped(job)
        self.assertFalse(Path(job['archive_layout']['targets']['SRT']).exists())
        self.assertFalse(Path(job['archive_layout']['targets']['SRT']).parent.exists())


if __name__=='__main__':unittest.main()
