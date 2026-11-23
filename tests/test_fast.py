from __future__ import annotations
import copy
import csv
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.fast import check_pair, compose_bundle, compose_csv, compose_pair, stream_evidence
from jyexport.storage import sha


class FastGuardTests(unittest.TestCase):
    def evidence(self):
        return {kind: {'metadata': {'codec_name': codec, 'time_base': '1/60'},
                       'time_sha256': 'same-clock'}
                for kind, codec in [('video', 'h264'), ('audio', 'aac')]}

    def test_rejects_audio_rate_mismatch(self):
        a = self.evidence(); b = copy.deepcopy(a)
        a['audio']['metadata']['sample_rate'] = '48000'
        b['audio']['metadata']['sample_rate'] = '44100'
        with self.assertRaisesRegex(ValueError, 'sample_rate'): check_pair(a,b)

    def test_rejects_packet_timeline_mismatch(self):
        a = self.evidence(); b = copy.deepcopy(a)
        b['video']['time_sha256'] = 'shifted-one-frame'
        with self.assertRaisesRegex(ValueError, '时序不一致'): check_pair(a,b)

    def test_rejects_different_color_interpretation(self):
        a = self.evidence(); b = copy.deepcopy(a)
        a['video']['metadata']['color_transfer'] = 'bt709'
        b['video']['metadata']['color_transfer'] = 'smpte2084'
        with self.assertRaisesRegex(ValueError, 'color_transfer'): check_pair(a,b)

    def test_requires_pair_confirmation_before_touching_files(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, '确认'):
                compose_bundle('missing2','missing3','missing-srt',root,'unused','test')
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_refuses_same_master(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, '独立'):
                compose_pair('same.mp4','same.mp4',root,'unused')

    def test_batch_continues_other_episodes_after_failure(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);file=base/'batch.csv'
            with file.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(['剧名','集号','V2','V3','SRT'])
                writer.writerows([['test','1','2.mp4','3.mp4','s.srt'],['test','2','b2.mp4','b3.mp4','b.srt']])
            with patch('jyexport.fast.compose_bundle',side_effect=[ValueError('bad timing'),{'directory':'complete-2'}]) as compose:
                result=compose_csv(file,base/'out','unused',True)
            self.assertEqual((result['completed'],result['failed']),(1,1))
            self.assertEqual(compose.call_count,2)
            self.assertEqual(compose.call_args_list[0].args[0],base/'2.mp4')


class FastMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')
        if not (cls.app/'ffmpeg.exe').is_file(): raise unittest.SkipTest('需要剪映FFmpeg进行媒体流测试')
        cls.temp=tempfile.TemporaryDirectory(prefix='jy_fast_media_');cls.base=Path(cls.temp.name)
        cls.v2=cls.base/'native_v2.mp4';cls.v3=cls.base/'native_v3.mp4'
        for path,frequency,caption in [(cls.v2,440,True),(cls.v3,880,False)]:
            command=[str(cls.app/'ffmpeg.exe'),'-nostdin','-v','error','-n','-f','lavfi','-i',
                'color=c=black:s=1080x1920:r=60:d=0.5','-f','lavfi','-i',f'sine=frequency={frequency}:sample_rate=48000:duration=0.5']
            if caption: command+=['-vf','drawbox=x=250:y=1400:w=580:h=80:color=white:t=fill']
            command+=['-c:v','mpeg4','-q:v','6','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-t','0.5',str(path)]
            result=subprocess.run(command,capture_output=True)
            if result.returncode: raise RuntimeError(result.stderr.decode('utf-8','replace'))
        cls.source_hashes=[sha(cls.v2),sha(cls.v3)]
        cls.srt=cls.base/'source.srt';cls.srt.write_text('1\n00:00:00,000 --> 00:00:00,400\ntest\n',encoding='utf-8')

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_native_streams_and_timestamps_are_reused_exactly(self):
        folder=self.base/'derived'
        result=compose_pair(self.v2,self.v3,folder,self.app,500000)
        for key,vcode,acode in [('V1','V3','V2'),('V4','V2','V3')]:
            item=result['variants'][key]
            self.assertTrue(item['probe']['full_decode'])
            self.assertFalse(item['reviewed'])
            for kind,code in [('video',vcode),('audio',acode)]:
                self.assertEqual(item['streams'][kind]['packet_and_time_sha256'],result['sources'][code]['streams'][kind]['packet_and_time_sha256'])
        self.assertEqual([sha(self.v2),sha(self.v3)],self.source_hashes)
        with self.assertRaises(FileExistsError):compose_pair(self.v2,self.v3,folder,self.app)

    def test_bundle_has_five_files_and_keeps_review_pending(self):
        result=compose_bundle(self.v2,self.v3,self.srt,self.base/'bundles',self.app,'test__01',True)
        self.assertEqual(set(result['variants']),{'V1','V2','V3','V4'})
        self.assertEqual(result['srt']['cues'],1)
        self.assertTrue(Path(result['directory'],'审核归档总表.csv').is_file())
        self.assertTrue(all(not item['reviewed'] for item in result['variants'].values()))
        self.assertEqual(sha(result['variants']['V2']['file']),sha(self.v2))

    def test_output_inside_draft_is_rejected(self):
        root=self.base/'draft';root.mkdir();(root/'draft_content.json').write_text('encrypted')
        with self.assertRaisesRegex(ValueError,'草稿内部'):
            compose_bundle(self.v2,self.v3,self.srt,root/'out',self.app,'test',True)


if __name__=='__main__':unittest.main(verbosity=2)
