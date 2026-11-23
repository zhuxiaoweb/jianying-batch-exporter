import concurrent.futures
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jyexport.media import _decode_threads,decode_scheduling,probe


class DecodeSchedulingTests(unittest.TestCase):
    def test_cpu_limit_only_while_rendering_and_reset_on_error(self):
        state=[4]
        with patch('jyexport.media.os.cpu_count',return_value=12):
            self.assertEqual(_decode_threads(),0)
            with self.assertRaisesRegex(ValueError,'test'):
                with decode_scheduling(lambda:state[0]):
                    self.assertEqual(_decode_threads(),4)
                    state[0]=0;self.assertEqual(_decode_threads(),0)
                    raise ValueError('test')
            self.assertEqual(_decode_threads(),0)

    def test_low_core_cpu_limit(self):
        for count,expected in [(1,1),(6,2),(128,4),(None,1)]:
            with patch('jyexport.media.os.cpu_count',return_value=count),decode_scheduling(lambda:1):
                self.assertEqual(_decode_threads(),expected)

    def test_policy_is_isolated_between_validation_workers(self):
        barrier=threading.Barrier(2)
        def check(active):
            with decode_scheduling(lambda:active):
                barrier.wait();return _decode_threads()
        with patch('jyexport.media.os.cpu_count',return_value=12),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(check,1);b=pool.submit(check,0)
            self.assertEqual((a.result(),b.result()),(4,0))

    def test_full_audio_video_mapping_and_failure_checks_retained(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);movie=root/'test.mp4';movie.write_bytes(bytes(2048))
            (root/'ffprobe.exe').touch();(root/'ffmpeg.exe').touch()
            meta={'streams':[{'codec_type':'video','width':1080,'height':1920,'avg_frame_rate':'60/1'},
                             {'codec_type':'audio'}],'format':{'format_name':'mp4','duration':'1'}}
            with patch('jyexport.media.subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout=json.dumps(meta)),SimpleNamespace(returncode=1,stderr='decode error')]) as run:
                with decode_scheduling(lambda:1),self.assertRaisesRegex(ValueError,'完整解码检查失败'):
                    probe(movie,root,decode=True)
            args=run.call_args.args[0]
            self.assertIn('-xerror',args);self.assertEqual(args[args.index('-i')+1],str(movie))
            self.assertEqual(args[args.index('-i')+2:],['-map','0:v:0','-map','0:a:0','-f','null','-'])
            self.assertIn('-threads',args);self.assertLess(args.index('-threads'),args.index('-i'))

if __name__=='__main__':unittest.main()
