import tempfile
import unittest
from pathlib import Path

from jyexport.portable_test import _correlation, _tone, _wrap_periodic, run_test
from jyexport.processed_audio import decode_referenced_wave


class PortableTestTests(unittest.TestCase):
    def test_known_pcm_survives_wrapping_and_reader_without_using_decoder_as_oracle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'generated.wav'
            _tone(source,1.,440,.09,silence=.5)
            expected=source.read_bytes()
            digest=_wrap_periodic(source,78,196611,65539)
            result=decode_referenced_wave(source,Path(temporary)/'readable.wav',1000000)
            self.assertEqual(result['decoded_sha256'],digest)
            self.assertEqual(Path(result['decoded']).read_bytes(),expected)

    def test_audio_comparison_reports_small_lag_without_modifying_samples(self):
        import numpy as np
        rng=np.random.default_rng(3);expected=rng.normal(0,.05,44100)
        actual=np.r_[np.zeros(7),expected[:-7]];before=actual.copy()
        result=_correlation(expected,actual)
        self.assertGreater(result['correlation'],.99999)
        self.assertEqual(abs(result['lag_samples']),7)
        np.testing.assert_array_equal(actual,before)
        unrelated=rng.normal(0,.05,44100)
        self.assertLess(_correlation(expected,unrelated)['correlation'],.1)

    def test_failed_environment_has_durable_failure_report_and_no_fixtures(self):
        class MissingRuntime:
            def verify(self):raise ValueError('test runtime unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            report=run_test(MissingRuntime(),temporary,temporary)
            self.assertFalse(report['success'])
            self.assertIn('unavailable',report['error'])
            directory=Path(report['directory'])
            self.assertTrue((directory/'自测报告.json').is_file())
            self.assertFalse((directory/'生成素材').exists())
            self.assertFalse(report['user_draft_library_read'])
