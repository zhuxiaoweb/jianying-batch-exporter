import struct
import tempfile
import unittest
from pathlib import Path

from jyexport.media_crypto import _witness_boundary_candidates


def pcm_wave(samples, metadata=b'', channels=2, rate=44100, width=2):
    block = channels*width
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate*block, block, width*8)
    chunks = b'fmt '+struct.pack('<I', len(fmt))+fmt
    if metadata:
        chunks += b'JUNK'+struct.pack('<I', len(metadata))+metadata+b'\0'*(len(metadata)&1)
    chunks += b'data'+struct.pack('<I', len(samples))+samples+b'\0'*(len(samples)&1)
    return b'RIFF'+struct.pack('<I', len(chunks)+4)+b'WAVE'+chunks


class WitnessBoundaryCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)/'Resources'/'audioAlg'
        self.directory.mkdir(parents=True)
        self.source = self.directory/'sample.wav'
        self.witness = self.directory/'sample.wav_download.wav'
        self.samples = b''.join(struct.pack('<hh', n*13-3000, 2000-n*7) for n in range(256))
        self.key = 117

    def install(self, original, witness, step, length):
        encrypted = bytes(value^self.key if i%step < length else value
                          for i, value in enumerate(original))
        self.source.write_bytes(encrypted+b'\0'*68)
        self.witness.write_bytes(witness)
        return len(original)

    def test_one_transition_is_an_exact_source_offset(self):
        original = pcm_wave(self.samples)
        body = self.install(original, original, 2000, 503)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([503], []))

    def test_multiple_periods_return_exact_ends_and_starts(self):
        original = pcm_wave(self.samples)
        body = self.install(original, original, 257, 143)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key),
                         ([143, 400, 657, 914], [257, 514, 771, 1028]))

    def test_metadata_size_difference_uses_source_pcm_offset(self):
        original = pcm_wave(self.samples, b'source-only-metadata')
        witness = pcm_wave(self.samples, b'different')
        body = self.install(original, witness, 3000, 501)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([501], []))

    def test_unrelated_pcm_produces_no_candidates(self):
        original = pcm_wave(self.samples)
        changed = bytearray(self.samples)
        changed[500] ^= 1
        body = self.install(original, pcm_wave(bytes(changed)), 257, 143)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([], []))

    def test_format_or_frame_count_mismatch_produces_no_candidates(self):
        original = pcm_wave(self.samples)
        for witness in (pcm_wave(self.samples, channels=1),
                        pcm_wave(self.samples, rate=48000),
                        pcm_wave(self.samples[:-4])):
            with self.subTest(bytes=len(witness)):
                body = self.install(original, witness, 257, 143)
                self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([], []))

    def test_truncated_or_non_pcm_witness_produces_no_candidates(self):
        original = pcm_wave(self.samples)
        unsupported = bytearray(original)
        struct.pack_into('<H', unsupported, 20, 3)
        for witness in (original[:-4], bytes(unsupported), b'not a wave'):
            with self.subTest(bytes=len(witness)):
                body = self.install(original, witness, 257, 143)
                self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([], []))

    def test_encrypted_sidecar_never_supplies_boundary_hints(self):
        original = pcm_wave(self.samples)
        body = self.install(original, bytes(v^35 for v in original), 257, 143)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([], []))

    def test_fully_encrypted_pcm_has_no_invented_boundary(self):
        original = pcm_wave(self.samples)
        body = self.install(original, original, 3000, 2000)
        self.assertEqual(_witness_boundary_candidates(self.source, body, self.key), ([], []))


if __name__ == '__main__':
    unittest.main()
