"""Unproved legacy PCM cannot inherit a safe peak or a completed receipt."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.advanced import (assert_decoder_cache_safe, decoder_cache_issues,
                              delivery_plan, prepare_resources)
from jyexport.discovery import Draft
from jyexport.engine import persist_manual_roles, render_job
from jyexport.storage import atomic_json, create_session, save_session, sha
from tests.test_advanced import Reader, document
from tests.test_processed_audio import encoded_wave, wave_bytes
from tests import test_series_publication236 as publication


def unsafe_preparation(group='media_results'):
    return {group:{'bindings':[{'wave':{'source':'source.wav',
        'source_sha256':'source-sha','decoded':'decoded.wav','decoded_sha256':'decoded-sha',
        'media_format_plan':{'method':3,'mode':'full_xor',
            'parameter_commitment_verified':False,'self_describing_full_xor':True,
            'validation':'self_describing_full_xor_pcm_v1'}}}]}}


class DecoderCache245Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_both_binding_groups_reject_structural_and_unproved_fullxor_receipts(self):
        for group in ('processing','media_results'):
            for marker in ('flag','validation','unproved'):
                with self.subTest(group=group,marker=marker):
                    prep=unsafe_preparation(group)
                    plan=prep[group]['bindings'][0]['wave']['media_format_plan']
                    if marker!='flag':plan.pop('self_describing_full_xor')
                    if marker!='validation':plan.pop('validation')
                    self.assertEqual(len(decoder_cache_issues({'preparation':prep})),1)
                    with self.assertRaisesRegex(ValueError,'旧声音缓存'):assert_decoder_cache_safe(prep)

    def test_committed_and_independently_verified_history_keeps_legacy_plan(self):
        job={'roles':{'voice':'保留原声音'},'want_srt':True}
        before=delivery_plan(job)
        plans=[{'mode':'plain','parameter_commitment_verified':False},
            {'mode':'full_xor','method':1,'parameter_commitment_verified':True},
            {'mode':'periodic_xor','method':3,'parameter_commitment_verified':True},
            {'mode':'full_xor','method':3,'parameter_commitment_verified':False,
             'validation':'complete_plaintext_pcm_witness_v1','pcm_witness':{'entire_pcm_equal':True}},
            {'mode':'full_xor','method':3,'parameter_commitment_verified':False,
             'validation':'verified_complete_plaintext_fingerprint_v1','pcm_profile':{'source_sha256':'known'}}]
        for plan in plans:
            job['headless']={'preparation':{'media_results':{'bindings':[{'wave':{'media_format_plan':plan}}]}}}
            self.assertEqual(decoder_cache_issues(job['headless']),[])
            self.assertEqual(delivery_plan(job),before)

    def test_preparation_refuses_unchanged_but_unproved_decoded_pcm(self):
        source=self.root/'source';(source/'Resources/audioAlg').mkdir(parents=True)
        raw=source/'Resources/audioAlg/voice.wav';raw.write_bytes(encoded_wave(wave_bytes(1.)))
        for name in ('music.mp3','effect.mp3'):(source/name).write_bytes(b'state-only media')
        run=self.root/'run';run.mkdir()
        _,record=prepare_resources(document(source),source,run)
        wave=record['media_results']['bindings'][0]['wave']
        wave['media_format_plan']=unsafe_preparation()['media_results']['bindings'][0]['wave']['media_format_plan']
        manifest=run/'advanced-preparation.json';atomic_json(manifest,record)
        before={str(p):sha(p) for p in (raw,Path(wave['decoded']),manifest)}
        with self.assertRaisesRegex(ValueError,'旧声音缓存'):
            prepare_resources(document(source),source,run)
        self.assertEqual(before,{p:sha(p) for p in before})

    def sealed_job(self):
        fixture=publication.SeriesPublicationTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture,fixture.session['jobs'][0]

    def test_completed_low_peak_delivery_cannot_bypass_decoder_proof(self):
        from jyexport.delivery_service import verified_delivery
        fixture,job=self.sealed_job();job['headless']['preparation']=unsafe_preparation()
        (fixture.root/'ffmpeg.exe').write_bytes(b'state-only validator')
        before={key:sha(item['file']) for key,item in job['variants'].items()}
        with patch('jyexport.audio_diagnostics.assert_audio_safe',return_value={'safe':True}) as audio, \
                patch('jyexport.series_archive.publish_grouped') as publish:
            with self.assertRaisesRegex(ValueError,'旧声音缓存'):verified_delivery(job,fixture.root)
        audio.assert_not_called();publish.assert_not_called()
        self.assertEqual(before,{key:sha(item['file']) for key,item in job['variants'].items()})

    def test_publication_and_already_published_reuse_both_reject_unproved_pcm(self):
        from jyexport.series_archive import publish_grouped
        fixture,job=self.sealed_job()
        before={key:sha(item['file']) for key,item in job['variants'].items()}
        job['headless']['preparation']=unsafe_preparation('processing')
        for published in (False,True):
            job['headless']['grouped_published']=published
            with self.assertRaisesRegex(ValueError,'旧声音缓存'):publish_grouped(job)
        self.assertEqual(before,{key:sha(item['file']) for key,item in job['variants'].items()})

    def test_stale_session_cannot_hide_unsafe_disk_preparation_or_journal(self):
        from jyexport.delivery_service import verified_delivery
        fixture,job=self.sealed_job()
        run=fixture.batch/'后台引擎工作区'/'legacy';run.mkdir(parents=True)
        job['headless']['run_directory']=str(run)
        for name,value in [('advanced-preparation.json',unsafe_preparation()),
                           ('run.json',{'preparation':unsafe_preparation('processing')})]:
            path=run/name;atomic_json(path,value)
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'旧声音缓存'):
                verified_delivery(job)
            path.unlink()


class DecoderCacheResume245Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source=self.root/'source';(self.source/'Resources/audioAlg').mkdir(parents=True)
        (self.source/'Resources/audioAlg/voice.wav').write_bytes(encoded_wave(wave_bytes(1.)))
        for name in ('music.mp3','effect.mp3'):(self.source/name).write_bytes(b'state-only media')
        content=self.source/'draft_content.json';content.write_text(json.dumps(document(self.source)),'utf-8')
        draft=Draft('case','case',str(self.source),str(content),'episode','1',False,3000000)
        self.folder,self.session=create_session([draft],self.root/'out',['V1','V2','V3','V4'],True)
        self.job=self.session['jobs'][0];self.adapter=Reader(self.root,self.root)
        data=self.adapter.read(self.job['backup_content'])
        persist_manual_roles(self.folder,self.session,self.job,self.adapter.classify_tracks(data),
                             sha(self.job['backup_content']),'scoped_v1')
        self.tools=self.root/'tools';self.tools.mkdir()
        for name in ('ffmpeg.exe','ffprobe.exe'):(self.tools/name).write_bytes(b'state-only validator')
        self.calls=[]

    def render(self,content,output,*args,**kwargs):
        content=Path(content);self.calls.append((content.parent,content.stem))
        if content.stem=='V3':raise RuntimeError('deliberate second master failure')
        Path(output).write_bytes(b'low-peak state-only master')
        return {'input_sha256':sha(content),'output_sha256':sha(output),'exit_code':0,
                'audio_diagnostics':{'safe':True}}

    def attempt(self):
        with patch.object(self.adapter,'render',side_effect=self.render), \
                patch('jyexport.advanced.probe',return_value={}), \
                patch('jyexport.advanced.probe_media_inputs',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'deliberate'):
                render_job(self.folder,self.session,self.job,self.adapter,self.tools,1)

    def poison_saved_preparation(self):
        run=Path(self.job['headless']['run_directory'])
        journal=run/'run.json';saved=json.loads(journal.read_text('utf-8'))
        saved['preparation']['media_results']['bindings'][0]['wave']['media_format_plan']= \
            unsafe_preparation()['media_results']['bindings'][0]['wave']['media_format_plan']
        atomic_json(journal,saved)
        return run

    def test_unsafe_old_master_starts_new_isolated_run_and_preserves_old_results(self):
        self.attempt();old=self.poison_saved_preparation()
        before={str(p):sha(p) for p in old.rglob('*') if p.is_file()}
        self.attempt();new=Path(self.job['headless']['run_directory'])
        self.assertNotEqual(new,old)
        self.assertEqual(self.calls.count((old,'V2')),1)
        self.assertEqual(self.calls.count((new,'V2')),1)
        self.assertEqual(before,{p:sha(p) for p in before})
        migration=self.job['headless']['decoder_cache_migration']
        self.assertEqual(migration['previous_run_directory'],str(old))
        self.assertTrue(migration['previous_results_preserved'])
        self.assertEqual(decoder_cache_issues(self.job['headless']),[])

    def test_running_old_worker_is_still_checked_before_cache_migration(self):
        self.attempt();old=self.poison_saved_preparation()
        process=old/'process-legacy';process.mkdir()
        atomic_json(process/'process.json',{'state':'running','pid':12345})
        before=list((self.folder/'后台引擎工作区').iterdir())
        with patch('jyexport.advanced._process_alive',return_value=True), \
                self.assertRaisesRegex(ValueError,'仍有后台进程'):
            render_job(self.folder,self.session,self.job,self.adapter,self.tools,1)
        self.assertEqual(before,list((self.folder/'后台引擎工作区').iterdir()))

    def test_session_unsafe_proof_cannot_be_hidden_by_a_safe_older_journal(self):
        self.attempt();old=Path(self.job['headless']['run_directory'])
        self.job['headless']['preparation']=copy.deepcopy(unsafe_preparation())
        save_session(self.folder,self.session)
        self.attempt()
        self.assertNotEqual(Path(self.job['headless']['run_directory']),old)
        self.assertEqual(self.calls.count((old,'V2')),1)


if __name__=='__main__':unittest.main()
