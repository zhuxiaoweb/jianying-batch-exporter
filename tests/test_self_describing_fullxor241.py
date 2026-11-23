import hashlib
import io
import struct
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from jyexport import media_crypto
from jyexport.media_crypto import wave_plan
from jyexport.processed_audio import decode_referenced_wave


def pcm_wave(frames):
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setparams((2, 2, 44100, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00\xfe\xff' * frames)
    return out.getvalue()


def encoded_periodic(plain, key=43, step=260390, length=189591):
    raw = bytearray(plain)
    table = bytes(i ^ key for i in range(256))
    for start in range(0, len(raw), step):
        raw[start:start + length] = raw[start:start + length].translate(table)
    commitment = hashlib.sha256(struct.pack('>iiB', step, length, key)).digest()
    return bytes(raw) + footer(commitment)


def footer(commitment):
    return (struct.pack('>I', 68) + b'bdve' + struct.pack('>I', 48) + b'crpt'
            + struct.pack('>II', 1, 3) + commitment
            + struct.pack('>I', 12) + b'size' + struct.pack('>I', 68))


def bad_commitment_fullxor(plain, key=42):
    return plain.translate(bytes(i ^ key for i in range(256))) + footer(b'\x00' * 32)


class CommittedAudioAlgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'project' / 'Resources' / 'audioAlg' / 'result.wav'
        self.source.parent.mkdir(parents=True)
        self.output = self.root / 'decoded.wav'
        self.saved_parameters = dict(media_crypto._VERIFIED_PARAMETERS)
        media_crypto._VERIFIED_PARAMETERS.clear()

    def tearDown(self):
        media_crypto._VERIFIED_PARAMETERS.clear()
        media_crypto._VERIFIED_PARAMETERS.update(self.saved_parameters)
        self.temp.cleanup()

    def no_candidate_search(self):
        stack = ExitStack()
        for name in ('_boundaries', '_pcm_boundary_candidates', '_continuity_boundary_candidates'):
            stack.enter_context(patch.object(media_crypto, name, return_value=([], [])))
        stack.enter_context(patch.object(media_crypto, '_short_prefix_candidate', return_value=None))
        return stack

    def assert_source_preserved_and_no_pcm_published(self, fingerprint):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), fingerprint)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('decoded.wav.partial-*')), [])

    def test_aligned_audioalg_periodic_pcm_uses_real_b_parameters_and_preserves_clear_intervals(self):
        # Match the affected B human result's actual parameters and 1024-aligned
        # frame count. Full XOR preserves this WAVE's shape but corrupts PCM.
        plain = pcm_wave(1148928)
        encrypted = encoded_periodic(plain)
        self.source.write_bytes(encrypted)
        before = hashlib.sha256(encrypted).hexdigest()
        wrong = encrypted[:-68].translate(bytes(i ^ 43 for i in range(256)))
        with wave.open(io.BytesIO(wrong), 'rb') as audio:
            self.assertEqual(audio.getnframes(), 1148928)
        self.assertNotEqual(wrong, plain)
        self.assertEqual(media_crypto._VERIFIED_PARAMETERS, {})

        result = decode_referenced_wave(self.source, self.output, 26050000)

        self.assertEqual(self.output.read_bytes(), plain)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        plan = result['media_format_plan']
        self.assertEqual((plan['mode'], plan['step'], plan['length'], plan['key']),
                         ('periodic_xor', 260390, 189591, 43))
        self.assertTrue(plan['parameter_commitment_verified'])

    def test_valid_riff_and_aligned_pcm_without_committed_parameters_are_rejected(self):
        self.source.write_bytes(bad_commitment_fullxor(pcm_wave(11264)))
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.no_candidate_search():
            with self.assertRaisesRegex(ValueError, '参数未通过文件校验'):
                wave_plan(self.source)
            self.assertEqual(media_crypto._VERIFIED_PARAMETERS, {})
            with self.assertRaisesRegex(ValueError, '参数未通过文件校验'):
                decode_referenced_wave(self.source, self.output, 250000)
        self.assert_source_preserved_and_no_pcm_published(before)

    def test_narrow_clear_gap_recovers_real_ep11_parameters_without_a_warm_cache(self):
        plain = pcm_wave(1236992)
        encrypted = encoded_periodic(plain, key=139, step=205218, length=204928)
        self.source.write_bytes(encrypted)
        before = hashlib.sha256(encrypted).hexdigest()
        self.assertEqual(media_crypto._VERIFIED_PARAMETERS, {})

        result = decode_referenced_wave(self.source, self.output, 28050000)

        self.assertEqual(self.output.read_bytes(), plain)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        plan = result['media_format_plan']
        self.assertEqual((plan['mode'], plan['step'], plan['length'], plan['key']),
                         ('periodic_xor', 205218, 204928, 139))
        self.assertTrue(plan['parameter_commitment_verified'])

    def test_narrow_gap_unknown_commitment_still_stops_without_publishing_pcm(self):
        encrypted = bytearray(encoded_periodic(pcm_wave(1236992), key=139,
                                              step=205218, length=204928))
        encrypted[-44] ^= 1
        self.source.write_bytes(encrypted)
        before = hashlib.sha256(encrypted).hexdigest()
        # Exercise real narrow-gap discovery while bounding this negative
        # search. A timeout remains a rejection, never a decoding fallback.
        with patch('jyexport.processed_audio.wave_plan',
                   side_effect=lambda path: wave_plan(path, solver_seconds=0.1)):
            with self.assertRaises(ValueError):
                decode_referenced_wave(self.source, self.output, 28050000)
        self.assertEqual(media_crypto._VERIFIED_PARAMETERS, {})
        self.assert_source_preserved_and_no_pcm_published(before)

    def test_missing_candidates_cannot_publish_an_otherwise_committed_periodic_payload(self):
        self.source.write_bytes(encoded_periodic(pcm_wave(1148928)))
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.no_candidate_search():
            with self.assertRaisesRegex(ValueError, '参数未通过文件校验'):
                decode_referenced_wave(self.source, self.output, 26050000)
        self.assertEqual(media_crypto._VERIFIED_PARAMETERS, {})
        self.assert_source_preserved_and_no_pcm_published(before)

    def test_unverified_parameter_cache_entry_does_not_enable_full_xor(self):
        self.source.write_bytes(bad_commitment_fullxor(pcm_wave(11264)))
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        media_crypto._VERIFIED_PARAMETERS[(3, b'\x00' * 32, 42)] = (0, 45100)
        with self.no_candidate_search():
            with self.assertRaisesRegex(ValueError, '参数未通过文件校验'):
                decode_referenced_wave(self.source, self.output, 250000)
        self.assert_source_preserved_and_no_pcm_published(before)

    def test_complete_independent_plaintext_witness_remains_accepted(self):
        plain = pcm_wave(11264)
        self.source.write_bytes(bad_commitment_fullxor(plain))
        witness = self.source.with_name(self.source.name + '_download.wav')
        witness.write_bytes(plain)
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.no_candidate_search():
            result = decode_referenced_wave(self.source, self.output, 250000)
        self.assertEqual(self.output.read_bytes(), plain)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        plan = result['media_format_plan']
        self.assertEqual(plan['validation'], 'complete_plaintext_pcm_witness_v1')
        self.assertTrue(plan['pcm_witness']['entire_pcm_equal'])
        self.assertFalse(plan['parameter_commitment_verified'])


if __name__ == '__main__':
    unittest.main()
