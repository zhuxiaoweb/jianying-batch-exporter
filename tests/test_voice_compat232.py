"""Mode1 recognition and independent complete plaintext PCM witness checks."""
import copy,hashlib,io,json,struct,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack
from jyexport.processed_audio import final_audio_profile,decode_referenced_wave,bind_referenced_results
from jyexport.media_crypto import wave_plan
from jyexport.advanced import AdvancedAdapter,prepare_resources
from jyexport.delivery_service import prepare
from jyexport.discovery import Draft
from jyexport.storage import sha
from tests.test_advanced import Reader,document


class VoiceCompatibility232Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.project=self.root/'project'
        self.audio=self.project/'Resources/audioAlg';self.audio.mkdir(parents=True)
        self.source=self.audio/'result.wav';self.witness=Path(str(self.source)+'_download.wav')
        out=io.BytesIO()
        with wave.open(out,'wb') as stream:
            stream.setparams((2,2,44100,0,'NONE','not compressed'))
            stream.writeframes(struct.pack('<hh',1324,-712)*4410)
        self.plain=out.getvalue();key=129
        footer=(struct.pack('>I',68)+b'bdve'+struct.pack('>I',48)+b'crpt'+struct.pack('>II',1,3)
            +hashlib.sha256(struct.pack('>iiB',1000003,800011,key)).digest()
            +struct.pack('>I',12)+b'size'+struct.pack('>I',68))
        self.source.write_bytes(self.plain.translate(bytes(i^key for i in range(256)))+footer)
        self.witness.write_bytes(self.plain)
        self.destination=self.root/'work/out.wav'
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name,value in [('_boundaries',([],[])),('_pcm_boundary_candidates',([],[])),
                           ('_continuity_boundary_candidates',([],[])),('_short_prefix_candidate',None)]:
            self.stack.enter_context(patch('jyexport.media_crypto.'+name,return_value=value))

    def decode(self):return decode_referenced_wave(self.source,self.destination,100000)

    def test_mode1_and_mode2_keep_distinct_profiles_and_reject_ambiguous_states(self):
        node={'type':'vocal_separation','choice':1,'final_algorithm':'vocal_separate',
            'production_path':'explicit.wav','time_range':{'duration':100000}}
        self.assertEqual(final_audio_profile(node),'vocal_separate_choice1')
        node['choice']=2;self.assertEqual(final_audio_profile(node),'vocal_separate_choice2')
        for mutation in ({'choice':True},{'choice':0},{'choice':3},{'choice':'1'},
                         {'production_path':''},{'removed_sounds':['human']},{'final_algorithm':'unknown'}):
            other={**node,**mutation};self.assertIsNone(final_audio_profile(other))

    def test_witness_accepts_complete_equal_pcm_without_claiming_parameter_hash(self):
        a,b=sha(self.source),sha(self.witness);e=self.decode()
        self.assertEqual(self.destination.read_bytes(),self.plain)
        self.assertEqual((a,b),(sha(self.source),sha(self.witness)))
        plan=e['media_format_plan'];self.assertFalse(plan['parameter_commitment_verified'])
        self.assertTrue(plan['pcm_witness']['entire_pcm_equal'])
        self.assertEqual(plan['pcm_witness']['pcm_sha256'],e['pcm_sha256'])

    def test_different_riff_metadata_allowed_only_when_all_pcm_and_format_equal(self):
        padded=(self.plain[:4]+struct.pack('<I',len(self.plain)) + self.plain[8:36]
            +b'JUNK'+struct.pack('<I',0)+self.plain[36:])
        self.witness.write_bytes(padded)
        self.assertEqual(wave_plan(self.source)['validation'],'complete_plaintext_pcm_witness_v1')

    def test_one_changed_or_missing_sample_missing_witness_and_format_mismatch_reject(self):
        bad=bytearray(self.plain);bad[-1]^=1
        for data in (bytes(bad),self.plain[:-4],self.plain[:22]+struct.pack('<H',1)+self.plain[24:]):
            self.witness.write_bytes(data)
            with self.assertRaisesRegex(ValueError,'参数'):self.decode()
            self.assertFalse(self.destination.exists())
        self.witness.unlink()
        with self.assertRaisesRegex(ValueError,'参数'):self.decode()

    def test_bad_footer_is_not_rescued_by_matching_witness(self):
        raw=bytearray(self.source.read_bytes());raw[-1]^=1;self.source.write_bytes(raw)
        with self.assertRaisesRegex(ValueError,'封装结构'):self.decode()

    def test_source_modification_after_plan_does_not_publish_wrong_pcm(self):
        real=wave_plan
        def change(path):
            plan=real(path);raw=bytearray(self.source.read_bytes());raw[50]^=1;self.source.write_bytes(raw)
            return plan
        with patch('jyexport.processed_audio.wave_plan',side_effect=change):
            with self.assertRaises(ValueError):self.decode()
        self.assertFalse(self.destination.exists())

    def test_witness_modification_after_plan_does_not_publish(self):
        real=wave_plan
        def change(path):
            plan=real(path);self.witness.write_bytes(self.plain[:-4]);return plan
        with patch('jyexport.processed_audio.wave_plan',side_effect=change):
            with self.assertRaisesRegex(ValueError,'核对副本'):self.decode()
        self.assertFalse(self.destination.exists())

    def test_new_mode_binding_changes_only_result_path_and_cannot_pad_source_range(self):
        node={'id':'p','type':'vocal_separation','choice':1,'final_algorithm':'vocal_separate',
              'production_path':str(self.source),'time_range':{'start':1000000,'duration':100000}}
        segment={'id':'s','material_id':'m','source_timerange':{'start':1000000,'duration':100000},
                 'target_timerange':{'start':500000,'duration':100000},'extra_material_refs':['p'],'volume':.3}
        data={'id':'case','new_version':'191.0.0','tracks':[{'type':'audio','segments':[segment]}],
            'materials':{'audios':[{'id':'m','type':'music','path':'original.mp4'}],'vocal_separations':[node]}}
        original=copy.deepcopy(data);bound,report=bind_referenced_results(data,self.project,self.root/'bound')
        self.assertEqual(data,original);self.assertEqual(bound['tracks'],original['tracks'])
        bound['materials']['vocal_separations'][0]['production_path']=str(self.source)
        self.assertEqual(bound,original)
        segment['source_timerange']['duration']+=10000
        with self.assertRaises(ValueError):bind_referenced_results(data,self.project,self.root/'bad-range')

    def test_processing_error_records_actual_child_parameters_without_reading_audio(self):
        source=self.project/'draft_content.json';data=document(self.project)
        child=data['materials']['drafts'][0]['draft']
        child['materials']['vocal_separations']=[{'id':'unsupported','type':'vocal_separation',
            'choice':3,'final_algorithm':'vocal_separate','production_path':'MISSING.wav',
            'time_range':{'duration':1000000}}]
        child['tracks'][0]['segments'][0]['extra_material_refs']=['unsupported']
        source.write_text(json.dumps(data),'utf-8')
        adapter=Reader(self.root,self.root)
        drafts=[Draft('case','case',str(self.project),str(source),'episode','1',False,3000000)]
        folder,session=prepare(drafts,self.root/'batch',adapter)
        job=session['jobs'][0];self.assertIn('人声处理',job['easy_analysis_error'])
        diagnosis=json.loads(Path(job['compatibility_diagnostics']).read_text('utf-8'))
        self.assertEqual(diagnosis['issue'],'unsupported_active_processing')
        self.assertEqual(diagnosis['scope'],['compound'])
        self.assertEqual(diagnosis['vocal_processing'][0]['choice'],3)
        self.assertEqual(diagnosis['vocal_processing'][0]['references'][0]['segment_id'],'child-s')
        self.assertFalse(diagnosis['source_draft_modified'])

if __name__=='__main__':unittest.main()
