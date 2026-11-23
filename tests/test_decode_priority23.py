import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext

from jyexport.media import probe
from jyexport import media


def candidate_scheduling():
    return media.decode_scheduling(lambda:1) if hasattr(media,'decode_scheduling') else nullcontext()


class BackgroundDecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')
        if not (cls.app/'ffmpeg.exe').is_file():raise unittest.SkipTest('需要FFmpeg进行完整解码回归')
        cls.temp=tempfile.TemporaryDirectory(prefix='jy_decode_priority_');cls.base=Path(cls.temp.name)
        cls.movie=cls.base/'complete.mp4'
        p=subprocess.run([str(cls.app/'ffmpeg.exe'),'-nostdin','-v','error','-n','-f','lavfi','-i',
            'color=c=black:s=1080x1920:r=60:d=0.5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.5',
            '-c:v','mpeg4','-q:v','6','-threads','2','-pix_fmt','yuv420p','-c:a','aac','-t','0.5',str(cls.movie)],capture_output=True)
        if p.returncode:raise RuntimeError(p.stderr.decode('utf-8','replace'))

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_valid_audio_and_video_still_decode_completely(self):
        with candidate_scheduling():evidence=probe(self.movie,self.app,500000,decode=True)
        self.assertTrue(evidence['full_decode'])
        self.assertEqual((evidence['width'],evidence['height'],evidence['fps']),(1080,1920,'60'))

    def test_damaged_packet_with_valid_container_fails_full_decode(self):
        damaged=self.base/'damaged.mp4';shutil.copy2(self.movie,damaged)
        p=subprocess.run([str(self.app/'ffprobe.exe'),'-v','error','-select_streams','v:0','-show_packets',
            '-show_entries','packet=pos,size','-of','json',str(damaged)],capture_output=True,check=True)
        packets=json.loads(p.stdout)['packets'];packet=packets[len(packets)//2]
        with damaged.open('r+b') as stream:
            stream.seek(int(packet['pos']));stream.write(bytes(int(packet['size'])))
        # Metadata is still plausible; it must never stand in for decoding.
        self.assertFalse(probe(damaged,self.app,500000,decode=False)['full_decode'])
        with candidate_scheduling(),self.assertRaisesRegex(ValueError,'完整解码检查失败'):
            probe(damaged,self.app,500000,decode=True)

if __name__=='__main__':unittest.main()
