"""Generated tiny media and strict cached audio-gate receipt boundaries."""
import copy
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from jyexport.audio_diagnostics import (MAX_SAFE_PEAK_DB,audio_peak_diagnostics,
                                      assert_audio_safe,has_safe_audio_diagnostics)
from jyexport.storage import sha

APP=Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')


def float_wave(values):
    pcm=b''.join(struct.pack('<ff',v,v) for v in values)
    fmt=struct.pack('<HHIIHH',3,2,44100,44100*8,8,32)
    body=b'WAVEfmt '+struct.pack('<I',len(fmt))+fmt+b'data'+struct.pack('<I',len(pcm))+pcm
    return b'RIFF'+struct.pack('<I',len(body))+body


def receipt(**changes):
    diagnostics={'method':'ffmpeg_astats_full_decode_v1','safe':True,
                 'peak_dbfs':-1.,'rms_dbfs':-12.,'threshold_dbfs':.5,**changes}
    return {'renders':{key:{'audio_diagnostics':copy.deepcopy(diagnostics)} for key in ('V2','V3')}}


class AudioReceiptEdge247Tests(unittest.TestCase):
    def test_legacy_finite_receipts_remain_reusable_and_threshold_is_unchanged(self):
        self.assertEqual(MAX_SAFE_PEAK_DB,.5)
        self.assertTrue(has_safe_audio_diagnostics(receipt()))
        self.assertTrue(has_safe_audio_diagnostics(receipt(peak_dbfs=.5,rms_dbfs=-12.)))
        self.assertFalse(has_safe_audio_diagnostics(receipt(peak_dbfs=.500001)))

    def test_nonfinite_bool_string_and_nonnumeric_peak_or_rms_never_authorize_reuse(self):
        for field in ('peak_dbfs','rms_dbfs'):
            for value in (float('nan'),float('inf'),float('-inf'),'NaN','-1.0',False,True,None,[],{},-10**1000):
                with self.subTest(field=field,value=repr(value)):
                    self.assertFalse(has_safe_audio_diagnostics(receipt(**{field:value})))

    def test_silence_requires_paired_levels_explicit_marker_and_nonempty_clean_sample_count(self):
        clean={'peak_dbfs':'-inf','rms_dbfs':'-inf','digital_silence':True,
               'sample_count':4410,'nan_samples':0,'inf_samples':0}
        self.assertTrue(has_safe_audio_diagnostics(receipt(**clean)))
        json.dumps(receipt(**clean),allow_nan=False)
        for change in ({'rms_dbfs':-12.},{'digital_silence':False},{'sample_count':0},
                       {'sample_count':True},{'nan_samples':1},{'inf_samples':1}):
            with self.subTest(change=change):
                self.assertFalse(has_safe_audio_diagnostics(receipt(**{**clean,**change})))

    def test_new_sample_counters_cannot_be_partial_noninteger_or_nonzero_invalid_counts(self):
        for fields in ({'sample_count':4410},{'sample_count':4410,'nan_samples':0},
                       {'sample_count':4410,'nan_samples':0,'inf_samples':False},
                       {'sample_count':4410,'nan_samples':'0','inf_samples':0},
                       {'sample_count':4410,'nan_samples':1,'inf_samples':0}):
            with self.subTest(fields=fields):self.assertFalse(has_safe_audio_diagnostics(receipt(**fields)))

    def test_missing_counter_support_is_rejected_rather_than_claimed_clean(self):
        completed=subprocess.CompletedProcess([],0,'','Overall\nPeak level dB: -1\nRMS level dB: -12\n')
        with tempfile.TemporaryDirectory(prefix='jy_audio_schema_') as temporary:
            root=Path(temporary);source=root/'sample.wav';source.write_bytes(b'placeholder');(root/'ffmpeg.exe').touch()
            with patch('jyexport.audio_diagnostics.subprocess.run',return_value=completed):
                with self.assertRaisesRegex(ValueError,'未返回有效结果'):audio_peak_diagnostics(source,root)


@unittest.skipUnless((APP/'ffmpeg.exe').is_file(),'actual FFmpeg is unavailable')
class RealAudioGateEdge247Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_audio_edges_');self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'generated.wav'
        self.values=[.125*math.sin(2*math.pi*440*i/44100) for i in range(4410)]

    def check(self,body):
        self.source.write_bytes(body);before=sha(self.source)
        try:return audio_peak_diagnostics(self.source,APP)
        finally:self.assertEqual(sha(self.source),before)

    def test_real_digital_silence_is_accepted_with_strict_json_and_validated_samples(self):
        buf=io.BytesIO()
        with wave.open(buf,'wb') as f:
            f.setparams((2,2,44100,0,'NONE','not compressed'));f.writeframes(bytes(4410*4))
        result=self.check(buf.getvalue())
        self.assertTrue(result['safe']);self.assertTrue(result['digital_silence'])
        self.assertEqual((result['peak_dbfs'],result['rms_dbfs']),('-inf','-inf'))
        self.assertGreater(result['sample_count'],0)
        self.assertEqual((result['nan_samples'],result['inf_samples']),(0,0))
        json.dumps(result,allow_nan=False)
        self.assertTrue(has_safe_audio_diagnostics(receipt(**result)))

    def test_real_finite_control_is_safe_and_the_late_overrange_sample_still_blocks(self):
        self.assertTrue(self.check(float_wave(self.values))['safe'])
        changed=list(self.values);changed[-1]=2.
        result=self.check(float_wave(changed))
        self.assertFalse(result['safe']);self.assertGreater(result['peak_dbfs'],6.)
        with self.assertRaisesRegex(ValueError,'疑似削波'):assert_audio_safe(self.source,APP,'V2')

    def test_single_nan_inf_and_mixed_invalid_samples_never_pass_finite_audio(self):
        for specials in ((float('nan'),),(float('inf'),),(float('-inf'),),(float('nan'),float('inf'))):
            with self.subTest(specials=[repr(x) for x in specials]):
                changed=list(self.values)
                for index,value in enumerate(specials):changed[2205+index]=value
                with self.assertRaisesRegex(ValueError,'NaN|Inf'):self.check(float_wave(changed))


if __name__=='__main__':unittest.main()
