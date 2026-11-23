"""Cross-flow fixtures absent from the individual feature regressions.

Only generated PCM and state-only placeholders are used. These cases exercise
public preparation/confirmation APIs, not native audiovisual equivalence.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from jyexport.advanced import decoder_cache_issues, prepare_resources
from jyexport.delivery_service import analyze, confirm, prepare
from jyexport.discovery import Draft
from jyexport.scoped_variants import transform
from jyexport.storage import sha
from tests.test_advanced import Reader, document
from tests.test_processed_audio import encoded_wave, wave_bytes


class CombinedWorkflowCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='jy_workflow_compat_')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def test_grouped_video_only_confirms_both_omissions_and_resumes_without_empty_srt(self):
        source=self.root/'source';source.mkdir()
        media=source/'picture.mp4';media.write_bytes(b'state-only video placeholder, never rendered')
        data=document(source)
        video={'id':'picture-track','type':'video','segments':[{'id':'picture-segment',
            'material_id':'picture-material','source_timerange':{'start':0,'duration':3000000},
            'target_timerange':{'start':0,'duration':3000000},'volume':.37,
            'common_keyframes':[{'id':'volume-marker','property_type':'KFTypeVolume'}]}]}
        data['tracks']=[video]
        data['materials']={'videos':[{'id':'picture-material','type':'video','path':str(media)}]}
        content=source/'draft_content.json';content.write_text(json.dumps(data),'utf-8')
        draft=Draft('only-video','同名剧:工作场景',str(source),str(content),'episode','CON',False,3000000)
        adapter=Reader(self.root,self.root)
        folder,session=prepare([draft],self.root/'output',adapter,archive_mode='series')
        job=session['jobs'][0];original=sha(content)
        with self.assertRaisesRegex(ValueError,'视频原声'):
            confirm(folder,session,job,[],[],True,no_captions_confirmed=True)
        self.assertFalse(job['easy_confirmed'])
        confirm(folder,session,job,[],[],True,no_captions_confirmed=True,no_audio_confirmed=True)
        self.assertTrue(job['easy_confirmed']);self.assertFalse(job['want_srt'])
        self.assertTrue(job['delivery_options']['no_audio_tracks_confirmed'])
        analyze(folder,session,adapter)
        self.assertTrue(job['easy_confirmed']);self.assertFalse(job['want_srt'])
        self.assertEqual(Path(job['directory']).parent.name,'任务记录')
        self.assertFalse(any(folder.rglob('*.srt')))
        self.assertEqual(sha(content),original)
        self.assertEqual(sha(job['backup_content']),original)
        for key in ('V1','V2','V3','V4'):
            prepared=transform(data,job['roles'],key)
            self.assertEqual(prepared['tracks'][0],video)

    def test_each_supported_processing_profile_inside_compound_reuses_verified_preparation(self):
        profiles=[('human',{'removed_sounds':['human'],'final_algorithm':'instrument_separate'}),
            ('background',{'removed_sounds':['background'],'final_algorithm':'instrument_separate'}),
            ('choice1',{'choice':1,'final_algorithm':'vocal_separate'}),
            ('choice2',{'choice':2,'final_algorithm':'vocal_separate'})]
        for name,parameters in profiles:
            with self.subTest(profile=name):
                source=self.root/name;(source/'Resources/audioAlg').mkdir(parents=True)
                voice=source/'Resources/audioAlg/voice.wav'
                voice.write_bytes(encoded_wave(wave_bytes(1.)))
                result=source/'Resources/audioAlg/final.wav';result.write_bytes(wave_bytes(1.))
                for filename in ('music.mp3','effect.mp3'):(source/filename).write_bytes(b'state-only media')
                data=document(source);child=data['materials']['drafts'][0]['draft']
                node={'id':'final-result','type':'vocal_separation','production_path':str(result),
                      'time_range':{'start':0,'duration':1000000},**parameters}
                child['materials']['vocal_separations']=[node]
                child['tracks'][0]['segments'][0]['extra_material_refs']=['final-result']
                authored=copy.deepcopy(data);before={str(p):sha(p) for p in source.rglob('*') if p.is_file()}
                run=self.root/(name+'-run');run.mkdir()
                first,receipt=prepare_resources(data,source,run)
                second,reused=prepare_resources(data,source,run)
                self.assertEqual(first,second);self.assertEqual(receipt,reused)
                self.assertEqual(len(list(run.glob('prepare_*'))),1)
                self.assertEqual(decoder_cache_issues(receipt),[])
                self.assertEqual(data,authored)
                self.assertEqual(before,{path:sha(path) for path in before})
                bound=first['materials']['drafts'][0]['draft']['materials']['vocal_separations'][0]
                decoded=Path(bound['production_path'])
                self.assertTrue(decoded.is_relative_to(run))
                self.assertEqual(decoded.read_bytes(),result.read_bytes())
                restored=copy.deepcopy(bound);restored['production_path']=node['production_path']
                self.assertEqual(restored,node)
                roles=Reader(self.root,self.root).classify_tracks(data)
                for key in ('V1','V2','V3','V4'):
                    variant=transform(first,roles,key)
                    self.assertEqual(variant['materials']['drafts'][0]['draft']['tracks'],child['tracks'])


if __name__=='__main__':unittest.main()
