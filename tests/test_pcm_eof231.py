import copy
import io
import tempfile
import unittest
import wave
from pathlib import Path

from jyexport.processed_audio import decode_referenced_wave,map_processed_source_range
from jyexport.storage import sha


def pcm(frames):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as out:
        out.setnchannels(2);out.setsampwidth(2);out.setframerate(44100)
        out.writeframes(b'\x20\x00\xe0\xff'*frames)
    return stream.getvalue()


class PlainPcmEofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_real_reported_tail_patterns_keep_all_original_bytes_and_ranges(self):
        for index,(duration,frames) in enumerate([(16050000,707584),(28050000,1236992),
                (29050000,1281024),(5483333,241664),(22083334,973824),(8900000,392192)]):
            with self.subTest(duration=duration):
                source=self.root/(str(index)+'.wav');source.write_bytes(pcm(frames))
                info=decode_referenced_wave(source,self.root/(str(index)+'-copy.wav'),duration)
                segment={'id':'voice','source_timerange':{'duration':duration},'target_timerange':{'start':50600000,'duration':duration}}
                process={'time_range':{'duration':duration}};before=copy.deepcopy((segment,process,info))
                mapped=map_processed_source_range(segment,process,info)
                self.assertEqual(mapped['eof_tail_validation'],'plain_pcm_complete_buffer')
                self.assertGreater(mapped['authored_eof_tail_samples'],0)
                self.assertFalse(mapped['final_pcm_covers_authored_range'])
                self.assertEqual((segment,process,info),before)
                self.assertEqual(sha(source),info['decoded_sha256'])

    def test_header_or_pcm_truncation_is_rejected_before_any_tail_allowance(self):
        source=self.root/'truncated.wav';source.write_bytes(pcm(88064)[:-4])
        with self.assertRaises(ValueError):decode_referenced_wave(source,self.root/'result.wav',2000000)
        self.assertFalse((self.root/'result.wav').exists())

    def test_complete_wave_missing_a_whole_buffer_still_fails(self):
        source=self.root/'missing-buffer.wav';source.write_bytes(pcm(87040))
        info=decode_referenced_wave(source,self.root/'result.wav',2000000)
        with self.assertRaisesRegex(ValueError,'实际PCM'):
            map_processed_source_range({'source_timerange':{'duration':2000000}},
                {'time_range':{'duration':2000000}},info)

    def test_plausible_metadata_without_verified_file_identity_is_not_enough(self):
        segment={'source_timerange':{'duration':2000000}};process={'time_range':{'duration':2000000}}
        source=self.root/'voice.wav';source.write_bytes(pcm(88064))
        info=decode_referenced_wave(source,self.root/'copy.wav',2000000)
        for changes in ({'pcm_data_complete':False},{'plain_source_identity_verified':False},
                {'source_unchanged':False},{'decoded_sha256':'different'},
                {'media_format_plan':{'mode':'unknown','parameter_commitment_verified':False}},
                {'sample_format':'float32'},{'frames':88063}):
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'实际PCM'):
                map_processed_source_range(segment,process,{**info,**changes})

    def test_tail_cannot_extend_authored_processing_range_or_start_after_eof(self):
        source=self.root/'voice.wav';source.write_bytes(pcm(88064))
        info=decode_referenced_wave(source,self.root/'copy.wav',2000000)
        for source_range in ({'duration':2001000},{'start':1999999,'duration':100000}):
            with self.assertRaises(ValueError):
                map_processed_source_range({'source_timerange':source_range},
                    {'time_range':{'duration':2000000}},info)

if __name__=='__main__':unittest.main()
