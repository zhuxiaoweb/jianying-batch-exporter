"""Frozen grouped paths, Windows name collisions and legacy compatibility."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from jyexport.archive_layout import is_grouped,output_path_allowed,prepare_layout,project_directory,validate_layout
from jyexport.discovery import Draft
from jyexport.draft import VARIANTS
from jyexport.execution import plan_contract
from jyexport.storage import create_session,prepare_job,validate_session


class ArchiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.batch=self.root/'batch'
    def tearDown(self):self.temp.cleanup()
    def draft(self,name='同一部剧',episode='时间线01',index='one'):
        source=self.root/index;source.mkdir(exist_ok=True)
        content=source/'draft_content.json'
        if not content.exists():content.write_text('{}','utf-8')
        return Draft(index,name,str(source),str(content),'timeline-'+index,episode,False,1000000)
    def job(self,draft,jobid='0123456789',drafts=None):
        return {'id':jobid,'draft':draft.dict(),'directory':str(self.batch/'任务记录'/jobid),
            'archive_layout':prepare_layout(self.batch,draft,jobid,drafts or [draft])}

    def test_plain_structure_is_project_variants_and_real_srt(self):
        draft=self.draft();job=self.job(draft);layout=validate_layout(self.batch,job)
        self.assertTrue(is_grouped(job));self.assertEqual(project_directory(job),self.batch/'交付'/'同一部剧')
        for key,spec in VARIANTS.items():
            self.assertEqual(Path(layout['targets'][key]),self.batch/'交付'/'同一部剧'/(key+'_'+spec['label'])/'时间线01.mp4')
        self.assertEqual(Path(layout['targets']['SRT']),self.batch/'交付'/'同一部剧'/'字幕srt'/'时间线01.srt')

    def test_same_raw_project_name_groups_and_different_episodes_do_not_get_suffixes(self):
        a=self.draft(episode='01');b=self.draft(episode='02',index='two');drafts=[a,b]
        x=self.job(a,'1111111111',drafts);y=self.job(b,'2222222222',drafts)
        self.assertEqual(x['archive_layout']['project_directory'],y['archive_layout']['project_directory'])
        self.assertEqual(x['archive_layout']['basename'],'01');self.assertEqual(y['archive_layout']['basename'],'02')

    def test_project_cleanup_case_truncation_and_reserved_name_collisions_get_stable_hashes(self):
        pairs=[('A:B','A?B'),('SHOW','show'),('P'*66+'A','P'*66+'B'),('CON','_CON')]
        for i,(left,right) in enumerate(pairs):
            a=self.draft(left,'01',str(i)+'a');b=self.draft(right,'02',str(i)+'b')
            x=self.job(a,'1111111111',[a,b]);y=self.job(b,'2222222222',[a,b])
            self.assertNotEqual(x['archive_layout']['project_directory'].casefold(),y['archive_layout']['project_directory'].casefold())
            self.assertTrue(x['archive_layout']['project_disambiguated']);self.assertTrue(y['archive_layout']['project_disambiguated'])
            validate_layout(self.batch,x);validate_layout(self.batch,y)
            self.assertIn(hashlib.sha256(left.encode()).hexdigest()[:8],x['archive_layout']['project_directory'])

    def test_literal_project_title_matching_another_suffixed_title_is_disambiguated(self):
        name='A:B';generated='A_B__'+hashlib.sha256(name.encode()).hexdigest()[:8]
        drafts=[self.draft(name,index='a'),self.draft('A?B',index='b'),self.draft(generated,index='c')]
        paths=[self.job(d,str(i)*10,drafts)['archive_layout']['project_directory'].casefold() for i,d in enumerate(drafts,1)]
        self.assertEqual(len(set(paths)),3)

    def test_timeline_cleanup_and_case_collisions_use_stable_job_ids(self):
        for i,(left,right) in enumerate([('时间线01','时间线01'),('A:B','A?B'),('EP','ep'),('E'*66+'A','E'*66+'B'),('CON','_CON')]):
            a=self.draft(episode=left,index=str(i)+'a');b=self.draft(episode=right,index=str(i)+'b')
            x=self.job(a,'1111111111',[a,b]);y=self.job(b,'2222222222',[a,b])
            self.assertTrue(x['archive_layout']['timeline_disambiguated'])
            self.assertTrue(x['archive_layout']['basename'].endswith('__1111111111'))
            self.assertNotEqual(x['archive_layout']['targets']['V1'].casefold(),y['archive_layout']['targets']['V1'].casefold())
            validate_layout(self.batch,x);validate_layout(self.batch,y)

    def test_literal_episode_name_matching_generated_basename_is_handled_with_full_plan(self):
        a=self.draft(episode='01',index='a');b=self.draft(episode='01',index='b')
        c=self.draft(episode='01__2222222222',index='c');drafts=[a,b,c]
        ids=['1111111111','2222222222','3333333333'];planned=list(zip(drafts,ids))
        layouts=[prepare_layout(self.batch,d,i,drafts,planned_ids=planned) for d,i in planned]
        self.assertEqual(len({x['targets']['V1'].casefold() for x in layouts}),3)

    def test_worker_can_validate_a_single_job_without_other_selected_drafts(self):
        a=self.draft('A:B','same','a');b=self.draft('A?B','same','b')
        job=self.job(a,'1111111111',[a,b]);validate_layout(self.batch,job)
        self.assertTrue(output_path_allowed(job,'V1',job['archive_layout']['targets']['V1']))

    def test_only_private_outputs_or_exact_current_key_target_are_allowed(self):
        job=self.job(self.draft());private=Path(job['directory'])/'快速交付'/'V1.mp4'
        self.assertTrue(output_path_allowed(job,'V1',private))
        target=job['archive_layout']['targets']['V1'];self.assertTrue(output_path_allowed(job,'V1',target))
        self.assertFalse(output_path_allowed(job,'V2',target))
        self.assertFalse(output_path_allowed(job,'V1',Path(target).with_name('other.mp4')))
        self.assertFalse(output_path_allowed(job,'V1',self.root/'outside.mp4'))
        self.assertFalse(output_path_allowed(job,'V5',private))

    def test_layout_tampering_and_unknown_fields_are_rejected(self):
        original=self.job(self.draft())
        for change in ('directory','project','basename','target','mode','flag','missing','extra'):
            job=copy.deepcopy(original);layout=job['archive_layout']
            if change=='directory':job['directory']=str(self.batch/'任务记录'/'other')
            elif change=='project':layout['project_directory']=str(self.root/'elsewhere')
            elif change=='basename':layout['basename']='other'
            elif change=='target':layout['targets']['V1']=str(self.root/'outside.mp4')
            elif change=='mode':layout['mode']='future'
            elif change=='flag':layout['timeline_disambiguated']=1
            elif change=='missing':del layout['targets']['SRT']
            else:layout['arbitrary_path']='elsewhere'
            with self.assertRaises(ValueError,msg=change):validate_layout(self.batch,job)

    def test_linked_target_or_project_cannot_alias_another_episode(self):
        job=self.job(self.draft());layout=job['archive_layout']
        target=Path(layout['targets']['V1']);target.parent.mkdir(parents=True)
        actual=target.with_name('another.mp4');actual.write_bytes(b'state only')
        try:target.symlink_to(actual)
        except OSError:self.skipTest('host does not permit symlinks')
        with self.assertRaises(ValueError):validate_layout(self.batch,job)
        self.assertFalse(output_path_allowed(job,'V1',target))

    def test_excessive_final_path_has_actionable_error_before_render(self):
        draft=self.draft('剧'*65,'集'*65);job=self.job(draft)
        # A long root deterministically crosses Windows' conservative budget.
        batch=self.root/('r'*80)/'batch';job['directory']=str(batch/'任务记录'/job['id'])
        job['archive_layout']=prepare_layout(batch,draft,job['id'],[draft])
        with self.assertRaisesRegex(ValueError,'输出位置.*过长'):validate_layout(batch,job)

    def test_legacy_create_keeps_private_per_episode_under_delivery_and_old_contract_fields(self):
        draft=self.draft();folder,session=create_session([draft],self.root/'out',list(VARIANTS))
        job=session['jobs'][0]
        self.assertNotIn('archive_layout',job);self.assertTrue(Path(job['directory']).is_relative_to(folder/'交付'))
        validate_session(folder,session);self.assertNotIn('archive_layout',plan_contract(job))

    def test_series_create_places_private_records_outside_review_and_freezes_layout(self):
        a=self.draft(episode='01',index='a');b=self.draft(episode='01',index='b')
        folder,session=create_session([a,b],self.root/'out',list(VARIANTS),archive_mode='series')
        for job in session['jobs']:
            self.assertEqual(Path(job['directory']),folder/'任务记录'/job['id'])
            self.assertTrue((Path(job['directory'])/'剪映原生导出步骤.txt').is_file())
            self.assertTrue(job['archive_layout']['basename'].endswith('__'+job['id']))
        validate_session(folder,session)
        one=copy.deepcopy(session);one['jobs']=one['jobs'][:1];validate_session(folder,one)
        before=plan_contract(session['jobs'][0]);session['jobs'][0]['archive_layout']['basename']='changed'
        self.assertNotEqual(before,plan_contract(session['jobs'][0]))

    def test_manifest_allows_intermediate_and_final_paths_but_not_other_episode_files(self):
        draft=self.draft();folder,session=create_session([draft],self.root/'out',list(VARIANTS),archive_mode='series')
        job=session['jobs'][0];job['variants']['V1']['file']=str(Path(job['directory'])/'stage'/'V1.mp4')
        validate_session(folder,session)
        job['variants']['V1']['file']=job['archive_layout']['targets']['V1'];validate_session(folder,session)
        job['variants']['V1']['file']=str(Path(job['archive_layout']['targets']['V1']).with_name('foreign.mp4'))
        with self.assertRaises(ValueError):validate_session(folder,session)

    def test_duplicate_final_targets_fail_closed_even_when_jobs_have_same_raw_title(self):
        a=self.draft(episode='01',index='a');b=self.draft(episode='02',index='b')
        folder,session=create_session([a,b],self.root/'out',list(VARIANTS),archive_mode='series')
        duplicate=copy.deepcopy(session['jobs'][0]);session['jobs'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'重复归档目标'):validate_session(folder,session)

    def test_manual_prepare_rejects_grouped_without_reading_or_writing_source(self):
        job=self.job(self.draft());codec=Mock()
        with self.assertRaisesRegex(ValueError,'主界面继续制作'):prepare_job(self.batch,{},job,codec)
        codec.read.assert_not_called()


if __name__=='__main__':unittest.main()
