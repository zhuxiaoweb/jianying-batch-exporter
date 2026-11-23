import unittest
from pathlib import Path
from unittest.mock import Mock

from jyexport.processed_audio import final_audio_profile, map_processed_source_range
from jyexport.error_messages import describe_failure


class Ep16Witness239Tests(unittest.TestCase):
    def processing(self):
        return {'type':'vocal_separation','choice':2,'final_algorithm':'vocal_separate',
                'production_path':'result.wav','time_range':{'duration':2183333}}

    def wave_info(self, witnessed=True):
        plan={'mode':'full_xor','parameter_commitment_verified':False,
              'validation':'complete_plaintext_pcm_witness_v1'}
        if witnessed:
            plan['pcm_witness']={'entire_pcm_equal':True,'pcm_sha256':'pcm',
                'frames':96256,'rate':44100,'channels':2,'sample_width':2}
        return {'rate':44100,'frames':96256,'source_unchanged':True,
                'pcm_data_complete':True,'pcm_sha256':'pcm',
                'media_format_plan':plan}

    def test_complete_plaintext_witness_allows_choice2_aligned_eof_tail(self):
        result=map_processed_source_range({'id':'ep16','source_timerange':{'duration':2183333}},
                                          self.processing(),self.wave_info())
        self.assertEqual(result['authored_eof_tail_samples'],29)
        self.assertEqual(result['eof_tail_validation'],'witnessed_choice2_complete_buffer')

    def test_unwitnessed_full_xor_still_rejects_same_tail(self):
        with self.assertRaisesRegex(ValueError,'实际PCM数据'):
            map_processed_source_range({'id':'ep16','source_timerange':{'duration':2183333}},
                                       self.processing(),self.wave_info(False))

    def test_failure_messages_explain_reference_and_pcm_repairs(self):
        job={'draft':{'name':'勿善','timeline_name':'EP16'},'easy_analysis':{'media_references':[]}}
        pcm=describe_failure(ValueError('片段超出实际PCM数据：x（需要96285采样，实际96256采样）'),job,'delivery')
        ref=describe_failure(ValueError('未验证的处理须走原生流程：引用素材不存在：abc'),job,'analysis')
        self.assertEqual(pcm['kind'],'unverified_audio')
        self.assertIn('重新生成',pcm['message'])
        self.assertEqual(ref['kind'],'missing_media_reference')
        self.assertIn('重新链接',ref['message'])


if __name__=='__main__':
    unittest.main()
