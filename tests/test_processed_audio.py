import io
import copy
import hashlib
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from jyexport.processed_audio import bind_referenced_media_results,bind_referenced_results,decode_referenced_wave,map_processed_source_range
from jyexport.timeline_graph import documents
from jyexport.storage import sha


def wave_bytes(seconds=.1):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as audio:
        audio.setnchannels(2);audio.setsampwidth(2);audio.setframerate(44100)
        audio.writeframes(b'\x01\x00\xfe\xff'*round(44100*seconds))
    return stream.getvalue()


def encoded_wave(normal, key=43, step=0, length=0):
    method=3 if step else 1
    raw=bytearray(normal)
    if step:
        for start in range(0,len(raw),step):
            for i in range(start,min(start+length,len(raw))):raw[i]^=key
    else:
        raw=bytearray(value^key for value in raw)
    digest=hashlib.sha256(struct.pack('>iiB',step,length,key)).digest()
    return bytes(raw)+struct.pack('>I',68)+b'bdve'+struct.pack('>I',48)+b'crpt'+struct.pack('>II',1,method)+digest+struct.pack('>I',12)+b'size'+struct.pack('>I',68)


class ProcessedAudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.wav';self.destination=self.root/'result.wav'
    def tearDown(self):self.temp.cleanup()

    def test_known_obfuscated_result_decodes_to_exact_pcm_without_changing_source(self):
        normal=wave_bytes();self.source.write_bytes(encoded_wave(normal));before=sha(self.source)
        result=decode_referenced_wave(self.source,self.destination,100000)
        self.assertEqual(self.destination.read_bytes(),normal)
        self.assertEqual(sha(self.source),before)
        self.assertFalse(result['render_equivalence_verified'])

    def test_plain_pcm_wave_is_also_accepted(self):
        self.source.write_bytes(wave_bytes())
        decode_referenced_wave(self.source,self.destination,100000)
        self.assertEqual(self.source.read_bytes(),self.destination.read_bytes())

    def test_unknown_format_is_rejected(self):
        self.source.write_bytes(b'not-a-wave-format-file')
        with self.assertRaises(ValueError):decode_referenced_wave(self.source,self.destination)
        self.assertFalse(self.destination.exists())

    def test_wrong_duration_does_not_publish_final_result(self):
        self.source.write_bytes(wave_bytes())
        with self.assertRaises(ValueError):decode_referenced_wave(self.source,self.destination,5000000)
        self.assertFalse(self.destination.exists())

    def test_output_cannot_be_inside_existing_draft(self):
        self.source.write_bytes(wave_bytes());folder=self.root/'draft';folder.mkdir();(folder/'draft_content.json').write_text('{}')
        with self.assertRaises(ValueError):decode_referenced_wave(self.source,folder/'new.wav')

    def test_processed_time_offset_maps_to_exact_sample_window(self):
        mapping=map_processed_source_range({'source_timerange':{'start':24650000,'duration':300000}},
            {'time_range':{'start':21650000,'duration':4400000}},{'rate':44100,'frames':194560})
        self.assertEqual(mapping['cached_source_start_us'],3000000)
        self.assertEqual(mapping['first_sample_floor'],132300)
        self.assertEqual(mapping['end_sample_ceil'],145530)

    def test_duplicate_identical_audio_definitions_stay_consistent_after_binding(self):
        resource=self.root/'Resources'/'audioAlg';resource.mkdir(parents=True)
        source=resource/'duplicate.wav';source.write_bytes(wave_bytes(.1))
        data={'id':'duplicate-case','tracks':[{'type':'audio','segments':[{'id':'s','material_id':'same','source_timerange':{'start':0,'duration':100000}}]}],
              'materials':{'audios':[{'id':'same','type':'music','path':str(source),'duration':100000},
                                     {'id':'same','type':'music','path':str(source),'duration':100000}]}}
        bound,evidence=bind_referenced_media_results(data,self.root,self.root/'decoded-duplicates')
        values=[item['path'] for item in bound['materials']['audios']]
        self.assertEqual(values[0],values[1]);self.assertEqual(evidence['decoded_files'],1)

    def test_unprocessed_source_region_is_not_substituted(self):
        with self.assertRaises(ValueError):map_processed_source_range({'source_timerange':{'start':100,'duration':1000}},
            {'time_range':{'start':500,'duration':2000}},{'rate':44100,'frames':100000})

    def test_choice2_metadata_tail_requires_real_sample_coverage(self):
        node={'type':'vocal_separation','choice':2,'final_algorithm':'vocal_separate',
              'production_path':'explicit-final.wav','time_range':{'start':15083333,'duration':3208333}}
        segment={'source_timerange':{'start':15100000,'duration':3200000}}
        mapping=map_processed_source_range(segment,node,{'rate':44100,'frames':142336})
        self.assertEqual(mapping['cached_source_start_us'],16667)
        self.assertEqual(mapping['declared_tail_overrun_us'],8334)
        self.assertTrue(mapping['final_pcm_covers_authored_range'])
        with self.assertRaisesRegex(ValueError,'实际PCM'):
            map_processed_source_range(segment,node,{'rate':44100,'frames':141487})
        segment['source_timerange']['duration']+=20000
        with self.assertRaisesRegex(ValueError,'源范围'):
            map_processed_source_range(segment,node,{'rate':44100,'frames':200000})

    def test_choice2_node_binding_retains_choice_gain_and_authored_clip_range(self):
        self.source.write_bytes(encoded_wave(wave_bytes()))
        node={'id':'p','type':'vocal_separation','choice':2,'final_algorithm':'vocal_separate',
              'production_path':str(self.source),'time_range':{'start':1000000,'duration':100000}}
        data={'tracks':[{'type':'audio','segments':[{'id':'s','material_id':'m','extra_material_refs':['p'],
              'source_timerange':{'start':1020000,'duration':50000},'target_timerange':{'start':9000000,'duration':50000},
              'volume':.3,'common_keyframes':[{'property_type':'KFTypeVolume','id':'gain'}]}]}],
              'materials':{'audios':[{'id':'m','path':'original.mp4'}],'vocal_separations':[node]}}
        before=copy.deepcopy(data)
        bound,evidence=bind_referenced_results(data,self.root,self.root/'decoded')
        self.assertEqual(evidence['decoded_files'],1)
        self.assertEqual(data,before)
        bound['materials']['vocal_separations'][0]['production_path']=str(self.source)
        self.assertEqual(bound,before)

    def test_committed_final_audio_buffer_eof_keeps_timeline_and_reports_short_tail(self):
        segment={'id':'authored-tail','source_timerange':{'duration':30066666}}
        process={'time_range':{'duration':30066666}}
        info={'rate':44100,'frames':1325056,'media_format_plan':{'parameter_commitment_verified':True}}
        before=copy.deepcopy((segment,process,info))
        mapping=map_processed_source_range(segment,process,info)
        self.assertEqual(mapping['authored_eof_tail_samples'],884)
        self.assertFalse(mapping['final_pcm_covers_authored_range'])
        self.assertEqual(mapping['source_duration_us'],30066666)
        self.assertEqual((segment,process,info),before)
        for changed in ({'frames':1324032},{'frames':1325055},
                        {'media_format_plan':{'parameter_commitment_verified':False}}):
            with self.subTest(changed=changed):
                rejected={**info,**changed}
                with self.assertRaisesRegex(ValueError,'实际PCM'):
                    map_processed_source_range(segment,process,rejected)

    def test_other_choice_with_existing_result_remains_blocked(self):
        self.source.write_bytes(wave_bytes())
        node={'id':'p','type':'vocal_separation','choice':3,'final_algorithm':'vocal_separate',
              'production_path':str(self.source),'time_range':{'duration':100000}}
        data={'tracks':[{'segments':[{'material_id':'m','extra_material_refs':['p']}]}],
              'materials':{'audios':[{'id':'m','path':'original.mp4'}],'vocal_separations':[node]}}
        with self.assertRaisesRegex(ValueError,'模式尚未验证'):
            bind_referenced_results(data,self.root,self.root/'decoded')

    def test_unverified_structural_xor_cannot_authorize_a_short_pcm_tail(self):
        segment={'id':'old-structural-tail','source_timerange':{'duration':30066666}}
        process={'time_range':{'duration':30066666}}
        info={'rate':44100,'frames':1325056,'source_sha256':'old-source',
              'media_format_plan':{'mode':'full_xor','parameter_commitment_verified':False,
                                  'validation':'self_describing_full_xor_pcm_v1',
                                  'self_describing_full_xor':True,'source_sha256':'old-source'}}
        with self.assertRaisesRegex(ValueError,'实际PCM'):
            map_processed_source_range(segment,process,info)

    def test_nested_bindings_change_only_production_paths_and_share_decoded_file(self):
        self.source.write_bytes(encoded_wave(wave_bytes()))
        process={'id':'process','type':'vocal_separation','removed_sounds':['human'],
            'final_algorithm':'instrument_separate','production_path':str(self.source),
            'time_range':{'start':1000000,'duration':100000},'gain':.3}
        def doc(identifier):
            return {'id':identifier,'tracks':[{'id':'audio','type':'audio','segments':[
                {'id':'segment','material_id':'sound','extra_material_refs':['process'],
                 'source_timerange':{'start':1020000,'duration':50000},
                 'target_timerange':{'start':9000000,'duration':50000},'volume':.25}]}],
                'materials':{'audios':[{'id':'sound','path':'original.mp4'}],
                             'vocal_separations':[copy.deepcopy(process),
                                {'id':'inactive','type':'vocal_separation','choice':1,'production_path':'MISSING'}]}}
        original=doc('root');child=doc('child')
        original['materials']['drafts']=[{'id':'compound','type':'combination','draft':child}]
        original['tracks'].append({'id':'video','type':'video','segments':[{'id':'c','material_id':'compound'}]})
        before=copy.deepcopy(original);source_before=sha(self.source)
        bound,evidence=bind_referenced_results(original,self.root,self.root/'decoded')
        self.assertEqual(original,before)
        self.assertEqual(sha(self.source),source_before)
        self.assertEqual(evidence['decoded_files'],1)
        self.assertEqual({tuple(item['scope']) for item in evidence['bindings']},{(),('compound',)})
        for _,document in documents(bound):
            item=document['materials']['vocal_separations'][0]
            self.assertNotEqual(item['production_path'],str(self.source))
            item['production_path']=str(self.source)
        self.assertEqual(bound,original)

    def test_missing_result_for_enabled_processing_does_not_modify_input(self):
        data={'tracks':[{'segments':[{'material_id':'p'}]}],
              'materials':{'processing':[{'id':'p','type':'vocal_separation','choice':1}]}}
        before=copy.deepcopy(data)
        with self.assertRaisesRegex(ValueError,'最终结果引用'):
            bind_referenced_results(data,self.root,self.root/'decoded')
        self.assertEqual(data,before)

    def test_direct_processed_media_is_decoded_without_retiming_or_touching_unreferenced_cache(self):
        media=self.root/'Resources'/'audioAlg';media.mkdir(parents=True)
        source=media/'human.wav';source.write_bytes(encoded_wave(wave_bytes(),key=69))
        data={'tracks':[{'type':'audio','segments':[
            {'id':'a','material_id':'human','source_timerange':{'start':20000,'duration':50000},
             'target_timerange':{'start':5000000,'duration':50000},'volume':.8}]}],
            'materials':{'audios':[{'id':'human','path':str(source),'duration':100000,'type':'extract_music'},
                {'id':'unused','path':str(media/'missing.wav'),'duration':100000}]}}
        before=copy.deepcopy(data);digest=sha(source)
        result,evidence=bind_referenced_media_results(data,self.root,self.root/'decoded')
        self.assertEqual(evidence['decoded_files'],1)
        self.assertEqual(Path(result['materials']['audios'][0]['path']).read_bytes(),wave_bytes())
        self.assertEqual(result['tracks'],data['tracks'])
        result['materials']['audios'][0]['path']=str(source)
        self.assertEqual(result,before);self.assertEqual(data,before);self.assertEqual(sha(source),digest)

    def test_unverified_other_audioalg_directory_is_not_substituted(self):
        other=self.root/'other'/'Resources'/'audioAlg';other.mkdir(parents=True)
        source=other/'human.wav';source.write_bytes(encoded_wave(wave_bytes(),key=69))
        data={'tracks':[{'type':'audio','segments':[{'id':'a','material_id':'human'}]}],
              'materials':{'audios':[{'id':'human','path':str(source),'duration':100000}]}}
        result,evidence=bind_referenced_media_results(data,self.root,self.root/'decoded')
        self.assertEqual(result,data);self.assertEqual(evidence['decoded_files'],0)

    def test_explicit_typed_external_processed_media_uses_exact_reference(self):
        folder=self.root/'preset';folder.mkdir()
        source=folder/'final.wav';source.write_bytes(encoded_wave(wave_bytes(),key=69))
        (folder/'unreferenced.wav').write_bytes(b'not a supported audio result')
        data={'tracks':[{'type':'audio','segments':[{'id':'a','material_id':'human',
            'source_timerange':{'start':0,'duration':100000},'target_timerange':{'start':200000,'duration':100000}}]}],
            'materials':{'audios':[{'id':'human','type':'extract_music','path':str(source),'duration':100000}]}}
        bound,evidence=bind_referenced_media_results(data,self.root,self.root/'decoded')
        self.assertEqual(evidence['decoded_files'],1)
        self.assertEqual(Path(bound['materials']['audios'][0]['path']).read_bytes(),wave_bytes())
        self.assertEqual(bound['tracks'],data['tracks'])

    def test_periodic_file_preserves_clear_intervals_instead_of_xoring_entire_pcm(self):
        normal=bytearray(wave_bytes())
        normal[44:4444]=bytes(4400)
        normal=bytes(normal)
        self.source.write_bytes(encoded_wave(normal,key=78,step=1257,length=414))
        result=decode_referenced_wave(self.source,self.destination,100000)
        self.assertEqual(self.destination.read_bytes(),normal)
        self.assertEqual(result['media_format_plan']['step'],1257)
        self.assertEqual(result['media_format_plan']['length'],414)
        self.assertTrue(result['media_format_plan']['parameter_commitment_verified'])

    def test_unverified_global_xor_is_rejected_even_with_plausible_wave_header(self):
        self.source.write_bytes(bytes(value^43 for value in wave_bytes()))
        with self.assertRaisesRegex(ValueError,'拒绝整文件猜测'):
            decode_referenced_wave(self.source,self.destination,100000)
        self.assertFalse(self.destination.exists())

    def test_parameter_commitment_mismatch_cannot_publish_a_plausible_wave(self):
        raw=bytearray(encoded_wave(wave_bytes()));raw[-44]^=1
        self.source.write_bytes(raw)
        with self.assertRaises(ValueError):
            decode_referenced_wave(self.source,self.destination,100000)
        self.assertFalse(self.destination.exists())

    def test_non_silent_periodic_pcm_crosses_streaming_block_boundary_without_corruption(self):
        try:import numpy
        except ImportError:self.skipTest('Candidate parameter discovery needs NumPy')
        normal=wave_bytes(seconds=7)
        self.source.write_bytes(encoded_wave(normal,key=78,step=392376,length=151414))
        result=decode_referenced_wave(self.source,self.destination,7000000)
        self.assertEqual(self.destination.read_bytes(),normal)
        self.assertEqual(result['media_format_plan']['step'],392376)

    def test_high_xor_key_keeps_exact_pcm_when_magnitude_boundaries_are_displaced(self):
        import numpy as np
        from unittest.mock import patch
        stream=io.BytesIO();ticks=np.arange(7*44100)/44100
        sound=np.round(3500*np.sin(2*np.pi*213*ticks)+700*np.sin(2*np.pi*547*ticks)).astype('<i2')
        with wave.open(stream,'wb') as audio:
            audio.setparams((2,2,44100,0,'NONE','not compressed'));audio.writeframes(np.repeat(sound,2).tobytes())
        normal=stream.getvalue();self.source.write_bytes(encoded_wave(normal,key=253,step=482636,length=327228))
        with patch('jyexport.media_crypto._boundaries',return_value=([],[])),patch('jyexport.media_crypto._pcm_boundary_candidates',return_value=([327217],[482467])):
            result=decode_referenced_wave(self.source,self.destination,7000000)
        self.assertEqual(self.destination.read_bytes(),normal)
        self.assertTrue(result['media_format_plan']['parameter_commitment_verified'])

    def test_short_periodic_interval_requires_its_committed_period_and_preserves_clear_bytes(self):
        normal=wave_bytes(seconds=11.)
        self.source.write_bytes(encoded_wave(normal,key=241,step=888228,length=152))
        result=decode_referenced_wave(self.source,self.destination,11000000)
        self.assertEqual(self.destination.read_bytes(),normal)
        self.assertEqual(result['media_format_plan']['step'],888228)
        self.assertEqual(result['media_format_plan']['length'],152)


if __name__=='__main__':unittest.main()
