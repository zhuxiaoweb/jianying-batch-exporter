"""Exact fingerprint fallbacks never turn generic fully-XOR files into inputs."""
import copy,hashlib,io,struct,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch

from jyexport.media_crypto import wave_plan
from jyexport.processed_audio import decode_referenced_wave
from jyexport.storage import sha
from jyexport.verified_pcm import verified_pcm_plan,verify_profile_output
from tests.test_processed_audio import wave_bytes,encoded_wave


class VerifiedPCMTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'external-preset.wav';self.output=self.root/'decoded.wav'
        self.plain=wave_bytes(.12)
        self.key=22;self.step=100007;self.length=40000
        self.encrypted=encoded_wave(self.plain,self.key,self.step,self.length)
        self.source.write_bytes(self.encrypted)
        with wave.open(io.BytesIO(self.plain),'rb') as audio:
            frames=audio.getnframes();pcm=audio.readframes(frames)
        self.profile={'source_sha256':hashlib.sha256(self.encrypted).hexdigest(),
            'plaintext_sha256':hashlib.sha256(self.plain).hexdigest(),
            'pcm_sha256':hashlib.sha256(pcm).hexdigest(),'body_size':len(self.plain),'key':self.key,'method':3,
            'parameter_sha256':hashlib.sha256(struct.pack('>iiB',self.step,self.length,self.key)).hexdigest(),
            'rate':44100,'frames':frames,'channels':2,'sample_width':2}
        self.commitment=bytes.fromhex(self.profile['parameter_sha256'])

    def tearDown(self):self.temp.cleanup()

    def plan(self):return verified_pcm_plan(self.source,len(self.plain),self.key,3,self.commitment)

    def test_unknown_whole_xor_with_valid_riff_and_pcm_is_still_rejected(self):
        self.assertIsNone(self.plan())
        with self.assertRaisesRegex(ValueError,'分段参数未通过'):wave_plan(self.source)
        self.assertFalse(self.output.exists())

    def test_registered_exact_source_and_entire_plaintext_decode_without_physical_witness(self):
        before=sha(self.source)
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):
            info=decode_referenced_wave(self.source,self.output,120000)
        self.assertEqual(self.output.read_bytes(),self.plain);self.assertEqual(sha(self.source),before)
        plan=info['media_format_plan'];self.assertFalse(plan['parameter_commitment_verified'])
        self.assertFalse(plan['original_period_recovered']);self.assertNotIn('pcm_witness',plan)
        self.assertEqual(plan['validation'],'verified_complete_plaintext_fingerprint_v1')

    def test_name_and_directory_are_not_proof_but_identical_bytes_can_move(self):
        self.source.rename(self.root/'different-name.wav');self.source=self.root/'different-name.wav'
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):self.assertIsNotNone(self.plan())
        bad=bytearray(self.encrypted);bad[-69]^=1;self.source.write_bytes(bad)
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):self.assertIsNone(self.plan())

    def test_changed_footer_or_truncation_never_matches(self):
        for raw in (self.encrypted[:-1],self.encrypted[:-30]+bytes([self.encrypted[-30]^1])+self.encrypted[-29:]):
            self.source.write_bytes(raw)
            with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):
                with self.assertRaises(ValueError):wave_plan(self.source)

    def test_plaintext_file_digest_and_pcm_digest_both_required(self):
        for field in ('source_sha256','plaintext_sha256','pcm_sha256'):
            profile=dict(self.profile);profile[field]='0'*64
            with patch('jyexport.verified_pcm.PROFILES',(profile,)):self.assertIsNone(self.plan())

    def test_every_format_fact_is_required(self):
        for field in ('rate','frames','channels','sample_width','body_size','key','method'):
            profile=dict(self.profile);profile[field]+=1
            with patch('jyexport.verified_pcm.PROFILES',(profile,)):self.assertIsNone(self.plan())

    def test_wrong_expected_duration_still_stops_before_final_publication(self):
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):
            with self.assertRaisesRegex(ValueError,'时长'):decode_referenced_wave(self.source,self.output,1000000)
        self.assertFalse(self.output.exists())

    def test_final_output_is_rechecked_and_a_changed_working_copy_is_not_published(self):
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):
            original=verify_profile_output
            def changed(plan,source_digest,decoded,*args):
                raw=bytearray(Path(decoded).read_bytes());raw[-1]^=1;Path(decoded).write_bytes(raw)
                original(plan,source_digest,decoded,*args)
            with patch('jyexport.verified_pcm.verify_profile_output',side_effect=changed):
                with self.assertRaisesRegex(ValueError,'完整明文指纹'):decode_referenced_wave(self.source,self.output,120000)
        self.assertFalse(self.output.exists())

    def test_forged_profile_receipt_is_not_accepted(self):
        self.output.write_bytes(self.plain)
        with patch('jyexport.verified_pcm.PROFILES',(self.profile,)):
            plan=self.plan();plan['pcm_profile']['frames']+=1
            with self.assertRaisesRegex(ValueError,'完整明文指纹'):
                verify_profile_output(plan,self.profile['source_sha256'],self.output,44100,self.profile['frames'],2,2,self.profile['pcm_sha256'])

    def test_existing_plain_and_committed_waves_do_not_use_the_profile_path(self):
        for payload in (self.plain,encoded_wave(self.plain)):
            self.source.write_bytes(payload)
            with patch('jyexport.verified_pcm.verified_pcm_plan',side_effect=AssertionError('profile not needed')):
                decode_referenced_wave(self.source,self.output,120000)
            self.assertEqual(self.output.read_bytes(),self.plain);self.output.unlink()


if __name__=='__main__':unittest.main()
