import tempfile
import unittest
from pathlib import Path

from jyexport.media_inputs import assert_readable_wave_paths,required_media_snapshot,probe_media_inputs


class MediaInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def document(self,path):
        return {'tracks':[{'type':'audio','segments':[{'id':'s','material_id':'a'}]}],
                'materials':{'audios':[{'id':'a','path':str(path)}]}}
    def test_missing_media_is_not_successful_silent_render(self):
        with self.assertRaises(FileNotFoundError):
            required_media_snapshot(self.document(self.root/'missing.mp3'))
    def test_existing_obfuscated_wave_is_rejected_before_render(self):
        path=self.root/'voice.wav';path.write_bytes(b'not readable wave')
        with self.assertRaisesRegex(ValueError,'丢失声音'):
            required_media_snapshot(self.document(path))
    def test_referenced_child_media_is_checked(self):
        child=self.document(self.root/'missing.mp3')
        data={'tracks':[{'type':'audio','segments':[{'material_id':'shell','extra_material_refs':['c']}]}],
              'materials':{'audios':[{'id':'shell','path':''}],
                           'drafts':[{'id':'c','type':'combination','draft':child}]}}
        with self.assertRaises(FileNotFoundError):
            required_media_snapshot(data)

    def test_wave_header_without_all_declared_samples_stops_before_render(self):
        import wave
        path=self.root/'short.wav'
        with wave.open(str(path),'wb') as audio:
            audio.setparams((2,2,44100,0,'NONE','not compressed'));audio.writeframes(bytes(10000))
        path.write_bytes(path.read_bytes()[:-8])
        with self.assertRaisesRegex(ValueError,'不完整'):
            assert_readable_wave_paths(self.document(path))

    def test_unused_unreadable_wave_does_not_block_another_material(self):
        data={'tracks':[{'type':'audio','segments':[]}],
              'materials':{'audios':[{'id':'unused','path':str(self.root/'missing.wav')}]}}
        assert_readable_wave_paths(data)

    def test_audio_track_cannot_use_a_file_with_only_video_stream(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        (self.root/'ffprobe.exe').write_bytes(b'unit test tool placeholder')
        media=self.root/'silent.mp4';media.write_bytes(b'unit test media placeholder')
        completed=SimpleNamespace(returncode=0,stdout=b'{"streams":[{"codec_type":"video","codec_name":"h264"}]}')
        with patch('jyexport.media_inputs.subprocess.run',return_value=completed):
            with self.assertRaisesRegex(ValueError,'媒体流'):
                probe_media_inputs(self.document(media),self.root)

    def test_audio_material_without_a_path_is_not_silently_ignored(self):
        data=self.document('');data['materials']['audios'][0]['path']=''
        with self.assertRaisesRegex(ValueError,'文件路径'):
            probe_media_inputs(data,self.root)

    def test_relocated_audio_must_cover_the_authored_source_range(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        (self.root/'ffprobe.exe').write_bytes(b'unit test tool placeholder')
        media=self.root/'chosen.mp3';media.write_bytes(b'unit test media placeholder')
        data=self.document(media);data['tracks'][0]['segments'][0]['source_timerange']={'start':200000,'duration':500000}
        completed=SimpleNamespace(returncode=0,stdout=b'{"streams":[{"codec_type":"audio"}],"format":{"duration":"0.5"}}')
        with patch('jyexport.media_inputs.subprocess.run',return_value=completed):
            with self.assertRaisesRegex(ValueError,'源范围'):probe_media_inputs(data,self.root)

    def test_still_photo_is_not_limited_to_one_probed_frame_duration(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        (self.root/'ffprobe.exe').write_bytes(b'unit test tool placeholder')
        media=self.root/'freeze.jpg';media.write_bytes(b'unit test media placeholder')
        data=self.document(media);data['tracks'][0]['type']='video'
        data['materials']['audios'][0]['type']='photo'
        data['tracks'][0]['segments'][0]['source_timerange']={'start':0,'duration':3000000}
        completed=SimpleNamespace(returncode=0,stdout=b'{"streams":[{"codec_type":"video","codec_name":"mjpeg"}],"format":{"duration":"0.04"}}')
        with patch('jyexport.media_inputs.subprocess.run',return_value=completed):
            result=probe_media_inputs(data,self.root)
        self.assertEqual(result[str(media)]['source_ranges'],[])
