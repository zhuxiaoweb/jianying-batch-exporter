"""An encrypted witness must prove its own parameters and every PCM byte."""
import io
import struct
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from jyexport import media_crypto
from jyexport.processed_audio import decode_referenced_wave
from jyexport.storage import sha
from tests.test_processed_audio import encoded_wave, wave_bytes

WITNESS_BOUNDARIES = media_crypto._witness_boundary_candidates


class CommittedWitness249Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'project/Resources/audioAlg'
        self.folder.mkdir(parents=True)
        self.source = self.folder / 'result.wav'
        self.witness = self.folder / 'result.wav_download.wav'
        self.output = self.root / 'decoded/out.wav'
        self.plain = wave_bytes()
        # Parameters exceed the body; structure alone cannot identify them.
        self.source.write_bytes(encoded_wave(self.plain, key=211, step=1000003, length=800011))
        self.witness.write_bytes(encoded_wave(self.plain, key=35))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        media_crypto._VERIFIED_PARAMETERS.clear()
        for name in ('_boundaries', '_pcm_boundary_candidates', '_continuity_boundary_candidates',
                     '_witness_boundary_candidates'):
            self.stack.enter_context(patch.object(media_crypto, name, return_value=([], [])))
        self.stack.enter_context(patch.object(media_crypto, '_short_prefix_candidate', return_value=None))

    def decode(self):
        return decode_referenced_wave(self.source, self.output, 100000)

    def test_committed_encrypted_download_proves_every_sample_and_preserves_sources(self):
        before = sha(self.source), sha(self.witness)
        info = self.decode()
        self.assertEqual(self.output.read_bytes(), self.plain)
        self.assertEqual((sha(self.source), sha(self.witness)), before)
        plan = info['media_format_plan']
        self.assertFalse(plan['parameter_commitment_verified'])
        evidence = plan['pcm_witness']
        self.assertEqual(evidence['encoding'], 'parameter_committed_bdve')
        self.assertTrue(evidence['format_plan']['parameter_commitment_verified'])
        self.assertEqual(evidence['pcm_sha256'], info['pcm_sha256'])
        self.assertTrue(evidence['entire_pcm_equal'])

    def test_one_changed_sample_in_a_valid_committed_download_is_rejected(self):
        changed = bytearray(self.plain)
        changed[-1] ^= 1
        self.witness.write_bytes(encoded_wave(bytes(changed), key=35))
        with self.assertRaises(ValueError):
            self.decode()
        self.assertFalse(self.output.exists())

    def test_matching_structural_xor_download_without_commit_is_rejected(self):
        self.witness.write_bytes(encoded_wave(self.plain, key=35, step=1000099, length=800099))
        with self.assertRaises(ValueError):
            self.decode()
        self.assertFalse(self.output.exists())

    def test_wrong_download_commit_cannot_rescue_identical_pcm(self):
        bad = bytearray(self.witness.read_bytes())
        bad[-44] ^= 1
        self.witness.write_bytes(bad)
        with self.assertRaises(ValueError):
            self.decode()
        self.assertFalse(self.output.exists())

    def test_download_truncation_and_different_channel_format_reject(self):
        for plain in (self.plain[:-4], self.plain[:22] + struct.pack('<H', 1) + self.plain[24:]):
            self.witness.write_bytes(encoded_wave(plain, key=35))
            with self.assertRaises(ValueError):
                self.decode()
            self.assertFalse(self.output.exists())

    def test_unknown_source_with_a_different_witness_pcm_cannot_be_whole_xored(self):
        # Whole XOR would corrupt every clear interval, as in the old noise bug.
        self.source.write_bytes(encoded_wave(self.plain, key=211, step=4000, length=2000))
        with self.assertRaises(ValueError):
            self.decode()
        self.assertFalse(self.output.exists())

    def test_encrypted_witness_cannot_recurse_to_another_witness(self):
        self.witness.write_bytes(encoded_wave(self.plain, key=35, step=1000099, length=800099))
        self.witness.with_name(self.witness.name + '_download.wav').write_bytes(self.plain)
        with self.assertRaises(ValueError):
            self.decode()
        self.assertFalse(self.output.exists())

    def test_witness_solver_receives_remaining_budget_and_no_witness_permission(self):
        real = media_crypto.wave_plan
        with patch.object(media_crypto, 'wave_plan', wraps=real) as calls:
            plan = media_crypto._complete_pcm_witness(self.source, len(self.plain), 211, .125)
        self.assertIsNotNone(plan)
        self.assertEqual(calls.call_args.kwargs,
                         {'solver_seconds': .125, '_allow_pcm_witness': False})
        self.assertIsNone(media_crypto._complete_pcm_witness(self.source, len(self.plain), 211, 0))

    def test_witness_change_after_planning_stops_publication(self):
        real = media_crypto.wave_plan
        def change(path):
            plan = real(path)
            self.witness.write_bytes(b'changed')
            return plan
        with patch('jyexport.processed_audio.wave_plan', side_effect=change):
            with self.assertRaisesRegex(ValueError, '核对副本'):
                self.decode()
        self.assertFalse(self.output.exists())

    def test_full_pcm_evidence_does_not_relax_authored_duration(self):
        with self.assertRaisesRegex(ValueError, '时长'):
            decode_referenced_wave(self.source, self.output, 5000000)
        self.assertFalse(self.output.exists())

    def test_exact_plaintext_boundary_recovers_commit_when_pcm_heuristics_miss_it(self):
        plain = wave_bytes(1.6)
        self.source.write_bytes(encoded_wave(plain, key=44, step=322382, length=129734))
        self.witness.write_bytes(plain)
        with patch.object(media_crypto, '_witness_boundary_candidates', side_effect=WITNESS_BOUNDARIES):
            info = decode_referenced_wave(self.source, self.output, 1600000)
        self.assertEqual(self.output.read_bytes(), plain)
        plan = info['media_format_plan']
        self.assertTrue(plan['parameter_commitment_verified'])
        self.assertEqual((plan['step'], plan['length']), (322382, 129734))

    def test_precise_plaintext_boundary_is_still_only_a_candidate(self):
        plain = wave_bytes(1.6)
        raw = bytearray(encoded_wave(plain, key=44, step=322382, length=129734))
        raw[-44] ^= 1
        self.source.write_bytes(raw)
        self.witness.write_bytes(plain)
        with patch.object(media_crypto, '_witness_boundary_candidates', side_effect=WITNESS_BOUNDARIES):
            with self.assertRaises(ValueError):
                decode_referenced_wave(self.source, self.output, 1600000)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
