import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class AudioDiagnostics213Tests(unittest.TestCase):
    def _paths(self, temporary):
        root = Path(temporary)
        source = root / 'sample.mp4'
        source.write_bytes(b'input')
        (root / 'ffmpeg.exe').write_bytes(b'tool')
        return source, root

    def test_parses_overrange_full_mix(self):
        from jyexport.audio_diagnostics import audio_peak_diagnostics

        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='',
            stderr='[Parsed_astats_0] Overall\n'
                   '[Parsed_astats_0] Peak level dB: 11.114246\n'
                   '[Parsed_astats_0] RMS level dB: -4.662439\n'
                   '[Parsed_astats_0] Number of samples: 4410\n'
                   '[Parsed_astats_0] Number of NaNs: 0.000000\n'
                   '[Parsed_astats_0] Number of Infs: 0.000000\n')
        with tempfile.TemporaryDirectory() as temporary:
            source, tools = self._paths(temporary)
            with patch('jyexport.audio_diagnostics.subprocess.run', return_value=completed) as run:
                result = audio_peak_diagnostics(source, tools)
        self.assertFalse(result['safe'])
        self.assertAlmostEqual(result['peak_dbfs'], 11.114246)
        self.assertEqual(run.call_args.args[0][0], str(tools.resolve() / 'ffmpeg.exe'))

    def test_safe_peak_is_accepted(self):
        from jyexport.audio_diagnostics import audio_peak_diagnostics

        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='',
            stderr='[Parsed_astats_0] Overall\n'
                   '[Parsed_astats_0] Peak level dB: -1.25\n'
                   '[Parsed_astats_0] RMS level dB: -12.0\n'
                   '[Parsed_astats_0] Number of samples: 4410\n'
                   '[Parsed_astats_0] Number of NaNs: 0.000000\n'
                   '[Parsed_astats_0] Number of Infs: 0.000000\n')
        with tempfile.TemporaryDirectory() as temporary:
            source, tools = self._paths(temporary)
            with patch('jyexport.audio_diagnostics.subprocess.run', return_value=completed):
                result = audio_peak_diagnostics(source, tools)
        self.assertTrue(result['safe'])

    def test_missing_peak_is_rejected(self):
        from jyexport.audio_diagnostics import audio_peak_diagnostics

        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as temporary:
            source, tools = self._paths(temporary)
            with patch('jyexport.audio_diagnostics.subprocess.run', return_value=completed):
                with self.assertRaisesRegex(ValueError, '未返回有效结果'):
                    audio_peak_diagnostics(source, tools)


if __name__ == '__main__':
    unittest.main()
