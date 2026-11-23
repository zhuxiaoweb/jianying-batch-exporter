"""Concurrency checkpoint, resource routing and cancellation regression.

All state/media is temporary. The real machine render mutex is mocked only in
the worker-routing unit test; no SDK or FFmpeg process is started here.
"""
import copy
from contextlib import contextmanager,nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from jyexport import advanced,delivery_service,storage
from jyexport.execution import BatchContext,ExecutionBudget
from jyexport.storage import atomic_json,sha,Cancelled
from tests import test_advanced as advanced_fixture
from tests import test_batch_execution as batch_fixture
from tests import test_series_publication236 as grouped_fixture


class FixtureCase(unittest.TestCase):
    def fixture(self,kind):
        value=kind();value.setUp()
        self.addCleanup(value.doCleanups);self.addCleanup(value.tearDown)
        return value


class PersistCandidateTests(FixtureCase):
    def context(self):
        fixture=self.fixture(grouped_fixture.SeriesPublicationTests)
        context=BatchContext(fixture.batch,fixture.session)
        self.addCleanup(context.close)
        local,job=context.worker(fixture.session['jobs'][0]['id'])
        return fixture,context,local,job

    def test_only_one_full_validation_and_it_is_the_real_save_session_validation(self):
        fixture,context,local,job=self.context();job['worker_marker']='checkpoint'
        real=storage.validate_session
        with patch('jyexport.execution.validate_session',side_effect=AssertionError('redundant pre-merge validation')), \
                patch('jyexport.storage.validate_session',wraps=real) as validated:
            context.persist(fixture.batch,local)
        self.assertEqual(validated.call_count,1)
        saved=json.loads((fixture.batch/'任务清单.json').read_text('utf-8'))
        self.assertEqual(saved['jobs'][0]['worker_marker'],'checkpoint')

    def test_invalid_noncontract_prepared_path_is_rejected_before_any_json_write(self):
        fixture,context,local,job=self.context()
        job['prepared']={'bad':{'folder':str(fixture.root/'outside')}}
        manifest=fixture.batch/'任务清单.json';before=manifest.read_bytes();real=storage.validate_session
        with patch('jyexport.storage.validate_session',wraps=real) as validated:
            with self.assertRaisesRegex(ValueError,'越界'):context.persist(fixture.batch,local)
        self.assertEqual(validated.call_count,1);self.assertEqual(manifest.read_bytes(),before)

    def test_wrong_exact_variant_destination_is_still_rejected_by_post_merge_validation(self):
        fixture,context,local,job=self.context();manifest=fixture.batch/'任务清单.json';before=manifest.read_bytes()
        # Preserve the old SHA to pass the stale-receipt guard; the full path
        # validator must still reject assigning V2 to V1's frozen destination.
        job['variants']['V2']['file']=job['archive_layout']['targets']['V1']
        with self.assertRaisesRegex(ValueError,'精确目标|归档文件'):context.persist(fixture.batch,local)
        self.assertEqual(manifest.read_bytes(),before)

    def test_duplicate_layout_targets_remain_rejected_without_writing(self):
        fixture,context,local,job=self.context();manifest=fixture.batch/'任务清单.json';before=manifest.read_bytes()
        job['archive_layout']['targets']['V2']=job['archive_layout']['targets']['V1']
        with self.assertRaisesRegex(ValueError,'不得改变'):context.persist(fixture.batch,local)
        self.assertEqual(manifest.read_bytes(),before)
        # save_session itself remains a full-batch guard as well.
        bad=copy.deepcopy(fixture.session)
        bad['jobs'][1]['archive_layout']['targets']['V1']=bad['jobs'][0]['archive_layout']['targets']['V1']
        with self.assertRaises(ValueError):storage.save_session(fixture.batch,bad)
        self.assertEqual(manifest.read_bytes(),before)

    def test_external_cas_change_is_never_overwritten_or_wastefully_revalidated(self):
        fixture,context,local,job=self.context();manifest=fixture.batch/'任务清单.json'
        external=json.loads(manifest.read_text('utf-8'));external['foreign_marker']='preserve me'
        atomic_json(manifest,external);before=manifest.read_bytes()
        with patch('jyexport.storage.validate_session',side_effect=AssertionError('CAS should fail first')):
            with self.assertRaisesRegex(ValueError,'其他程序'):context.persist(fixture.batch,local)
        self.assertEqual(manifest.read_bytes(),before)


class CompletedReviewBudgetTests(FixtureCase):
    def test_completed_review_uses_validation_but_new_fast_check_keeps_prepare(self):
        fixture=self.fixture(batch_fixture.BatchExecutionTests)
        fixture.session['jobs'][0]['headless']={'complete':True,'state':'complete'}
        storage.save_session(fixture.folder,fixture.session)
        budget=ExecutionBudget(4,validation_limit=2);seen=[]
        def verified(job,app):
            complete=bool((job.get('headless') or {}).get('complete'))
            seen.append((complete,budget.validation.snapshot()['active'],budget.prepare.snapshot()['active']))
            if complete:
                fixture.publish(job);return delivery_service.final_folder(job)
            return None
        with patch('jyexport.execution.ExecutionBudget',return_value=budget), \
                patch('jyexport.resources.machine_render_lock',return_value=nullcontext()), \
                patch('jyexport.delivery_service.verified_delivery',side_effect=verified), \
                patch('jyexport.delivery_service.render_job',side_effect=fixture.fake_render):
            result=delivery_service.run(fixture.folder,fixture.session,fixture.adapter,fixture.root,max_jobs=1)
        self.assertEqual(result['completed'],2)
        self.assertEqual(seen,[(True,1,0),(False,0,1)])


class Clock:
    def __init__(self):self.value=0.
    def now(self):return self.value
    def advance(self,seconds):self.value+=seconds


class Gate:
    def __init__(self,clock,wait=2.,cancel=False):
        self.clock=clock;self.wait=wait;self.cancel=cancel;self.active=0
    @contextmanager
    def slot(self,*args,**kwargs):
        self.clock.advance(self.wait)
        if self.cancel:raise Cancelled('controlled queued cancellation')
        self.active+=1
        try:yield
        finally:self.active-=1
    def snapshot(self):return {'active':self.active,'active_render':0}


class RestoredAudioBudgetTests(FixtureCase):
    def seed(self):
        fixture=self.fixture(advanced_fixture.AdvancedTests)
        folder,session=fixture.batch();job=session['jobs'][0]
        data=fixture.adapter.read(job['backup_content'])
        from jyexport.engine import persist_manual_roles
        persist_manual_roles(folder,session,job,fixture.adapter.classify_tracks(data),sha(job['backup_content']),'scoped_v1')
        app=fixture.root/'tools';app.mkdir()
        for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
        def render(source,target,*args,**kwargs):
            if Path(source).stem=='V3':raise RuntimeError('controlled incomplete second master')
            Path(target).write_bytes(b'state-only master')
            return {'input_sha256':sha(source),'output_sha256':sha(target)}
        with patch.object(fixture.adapter,'render',side_effect=render), \
                patch('jyexport.advanced.probe_media_inputs',return_value={}):
            with self.assertRaisesRegex(RuntimeError,'controlled'):
                advanced.render_advanced_job(folder,session,job,fixture.adapter,app,parallel=1)
        report=Path(job['directory'])/'restore-report.json';atomic_json(report,{'complete':True})
        job['headless'].update(bundle_report=str(report),bundle_report_sha256=sha(report))
        atomic_json(Path(job['headless']['run_directory'])/'run.json',job['headless'])
        return fixture,folder,session,job,app

    @staticmethod
    def adopted(*args,**kwargs):
        record=args[4];record.update(complete=True,state='complete');return record

    def test_restore_checks_share_validation_and_queue_time_is_separate_from_work(self):
        fixture,folder,session,job,app=self.seed();clock=Clock()
        budget=SimpleNamespace(prepare=Gate(clock),validation=Gate(clock),native=Gate(clock))
        def checked(*args):
            self.assertEqual(budget.validation.active,1);self.assertEqual(budget.prepare.active,0)
            clock.advance(3.)
            return {'safe':True,'method':'ffmpeg_astats_full_decode_v1','peak_dbfs':-1.,'rms_dbfs':-12.}
        with patch('jyexport.advanced.time.monotonic',side_effect=clock.now), \
                patch('jyexport.advanced.probe_media_inputs',return_value={}), \
                patch('jyexport.advanced.assert_audio_safe',side_effect=checked) as checks, \
                patch('jyexport.advanced._adopt_bundle',side_effect=self.adopted):
            result=advanced.render_advanced_job(folder,session,job,fixture.adapter,app,budget=budget)
        self.assertEqual(checks.call_count,2)
        self.assertEqual(result['phase_wait_seconds_this_attempt']['restore_audio_peak_diagnostics'],2.)
        self.assertEqual(result['phase_seconds_this_attempt']['restore_audio_peak_diagnostics'],6.)
        self.assertEqual((budget.prepare.active,budget.validation.active),(0,0))

    def test_canceled_restore_admission_records_wait_without_audio_start(self):
        fixture,folder,session,job,app=self.seed();clock=Clock()
        budget=SimpleNamespace(prepare=Gate(clock),validation=Gate(clock,cancel=True),native=Gate(clock))
        with patch('jyexport.advanced.time.monotonic',side_effect=clock.now), \
                patch('jyexport.advanced.probe_media_inputs',return_value={}), \
                patch('jyexport.advanced.assert_audio_safe') as checks:
            with self.assertRaises(Cancelled):
                advanced.render_advanced_job(folder,session,job,fixture.adapter,app,budget=budget)
        checks.assert_not_called();self.assertEqual(job['headless']['state'],'cancelled')
        self.assertEqual(job['headless']['phase_wait_seconds_this_attempt']['restore_audio_peak_diagnostics'],2.)

    def test_restore_without_budget_keeps_serial_fallback(self):
        fixture,folder,session,job,app=self.seed()
        with patch('jyexport.advanced.probe_media_inputs',return_value={}), \
                patch('jyexport.advanced.assert_audio_safe',return_value={'safe':True}) as checks, \
                patch('jyexport.advanced._adopt_bundle',side_effect=self.adopted):
            result=advanced.render_advanced_job(folder,session,job,fixture.adapter,app,budget=None)
        self.assertEqual(checks.call_count,2);self.assertTrue(result['complete'])


if __name__=='__main__':unittest.main()
