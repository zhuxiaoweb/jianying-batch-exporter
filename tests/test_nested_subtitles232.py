import copy,json,unittest
from jyexport.scoped_variants import inspect,scoped_key,subtitle_srt


def fixture():
    child={'id':'child','duration':2000000,'new_version':'191.0.0',
      'tracks':[{'id':'text','type':'text','segments':[{'id':'caption','material_id':'words',
          'target_timerange':{'start':250000,'duration':1000000}}]}],
      'materials':{'texts':[{'id':'words','type':'subtitle','content':'Final edited words'}]}}
    parent={'id':'parent','duration':8000000,'new_version':'191.0.0',
      'tracks':[{'id':'picture','type':'video','segments':[{'id':'first','material_id':'shell',
        'source_timerange':{'start':0,'duration':2000000},
        'target_timerange':{'start':3000000,'duration':2000000},'extra_material_refs':['compound','speed']}]}],
      'materials':{'videos':[{'id':'shell','type':'video','path':''}],
          'drafts':[{'id':'compound','type':'combination','draft':child}],
          'speeds':[{'id':'speed','type':'speed'}]}}
    return parent


def roles(data):return {r['key']:('字幕' if r['kind']=='text' else '保留原声音') for r in inspect(data)}


class NestedSubtitleTests(unittest.TestCase):
    def test_exact_instance_offset_and_no_source_mutation(self):
        data=fixture();before=copy.deepcopy(data)
        self.assertEqual(subtitle_srt(data,roles(data)),
            '1\n00:00:03,250 --> 00:00:04,250\nFinal edited words\n\n')
        self.assertEqual(data,before)

    def test_repeated_child_has_each_actual_instance_not_one_definition_time(self):
        data=fixture();seg=copy.deepcopy(data['tracks'][0]['segments'][0]);seg['id']='second';seg['target_timerange']['start']=6000000
        data['tracks'][0]['segments'].append(seg)
        result=subtitle_srt(data,roles(data));self.assertIn('00:00:06,250 --> 00:00:07,250',result)
        self.assertEqual(result.count('Final edited words'),2)

    def test_nested_offsets_accumulate_without_using_child_fps(self):
        data=fixture();child=data['materials']['drafts'][0]['draft'];inner=fixture()
        inner.update(id='middle',duration=2000000,fps=24)
        inner['tracks'][0]['segments'][0]['target_timerange']['start']=0
        data['materials']['drafts'][0]['draft']=inner
        self.assertIn('00:00:03,250 --> 00:00:04,250',subtitle_srt(data,roles(data)))

    def test_trim_duration_mismatch_and_parent_bounds_fail_closed(self):
        for field,value in [('source_start',1),('source_duration',1000000),('target_duration',1000000),('target_start',7000000)]:
            data=fixture();seg=data['tracks'][0]['segments'][0]
            interval,key=field.split('_');seg[interval+'_timerange'][key]=value
            with self.assertRaisesRegex(ValueError,'裁切'):subtitle_srt(data,roles(data))

    def test_reverse_constant_speed_and_curve_fields_fail_closed(self):
        for mutation in ({'reverse':True},{'is_reverse':True},{'speed':2},{'speed':True},{'curve_speed':{'x':1}}):
            data=fixture();data['tracks'][0]['segments'][0].update(mutation)
            with self.assertRaisesRegex(ValueError,'变速'):subtitle_srt(data,roles(data))
        for mutation in ({'speed':2},{'speed':True},{'curve_speed':{'x':1}},{'unknown_speed_option':'active'}):
            data=fixture();data['materials']['speeds'][0].update(mutation)
            with self.assertRaisesRegex(ValueError,'变速'):subtitle_srt(data,roles(data))

    def test_ambiguous_compound_reference_cannot_guess_caption_timeline(self):
        data=fixture();other=copy.deepcopy(data['materials']['drafts'][0]);other['id']='other'
        data['materials']['drafts'].append(other);data['tracks'][0]['segments'][0]['extra_material_refs'].append('other')
        with self.assertRaisesRegex(ValueError,'引用不明确'):subtitle_srt(data,roles(data))

    def test_hidden_instance_or_caption_does_not_appear(self):
        for parent in (True,False):
            data=fixture()
            if parent:data['tracks'][0]['segments'][0]['visible']=False
            else:data['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]['visible']=False
            self.assertEqual(subtitle_srt(data,roles(data)),'')

    def test_unselected_titles_stay_omitted_and_authored_content_preserved(self):
        data=fixture();child=data['materials']['drafts'][0]['draft'];segment=copy.deepcopy(child['tracks'][0]['segments'][0])
        segment.update(id='title-segment',material_id='title');child['tracks'][0]['segments'].append(segment)
        child['materials']['texts'].append({'id':'title','type':'text','content':'Title stays title'})
        selected=roles(data);selected[scoped_key(('compound',),'text:title-segment')]='标题/其他文字'
        self.assertNotIn('Title stays title',subtitle_srt(data,selected))

    def test_cues_are_clipped_to_their_authored_child_end_and_sorted(self):
        data=fixture();cue=data['materials']['drafts'][0]['draft']['tracks'][0]['segments'][0]
        cue['target_timerange']={'start':1500000,'duration':2000000}
        self.assertIn('00:00:04,500 --> 00:00:05,000',subtitle_srt(data,roles(data)))

    def test_audio_compound_cannot_claim_visible_child_subtitles(self):
        data=fixture();data['tracks'][0]['type']='audio'
        with self.assertRaisesRegex(ValueError,'引用不明确'):subtitle_srt(data,roles(data))


if __name__=='__main__':unittest.main()
