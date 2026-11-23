import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path

from jyexport.float_wave import inspect_float_wave,FLOAT_GUID
from jyexport.media_inputs import assert_readable_wave_paths
from jyexport.processed_audio import decode_referenced_wave


def floating_wave(extensible=False,invalid_sample=False):
    payload=struct.pack('<'+'f'*8820,*([float('nan') if invalid_sample else .125]*8820))
    fmt=struct.pack('<HHIIHH',65534 if extensible else 3,2,44100,352800,8,32)
    if extensible:fmt+=struct.pack('<HHI',22,32,3)+FLOAT_GUID
    body=b'WAVE'+b'fmt '+struct.pack('<I',len(fmt))+fmt+b'data'+struct.pack('<I',len(payload))+payload
    return b'RIFF'+struct.pack('<I',len(body))+body,payload


class FloatWaveTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def document(self,path):
        return {'tracks':[{'type':'audio','segments':[{'id':'s','material_id':'m'}]}],
                'materials':{'audios':[{'id':'m','path':str(path)}]}}

    def test_authored_float_pcm_is_readable_without_converting_any_sample(self):
        for extensible in (False,True):
            with self.subTest(extensible=extensible):
                path=self.root/('float'+str(extensible)+'.wav');raw,payload=floating_wave(extensible);path.write_bytes(raw)
                info=inspect_float_wave(path);self.assertEqual(info['frames'],4410)
                self.assertEqual(info['pcm_sha256'],hashlib.sha256(payload).hexdigest())
                assert_readable_wave_paths(self.document(path))
                destination=self.root/('copied'+str(extensible)+'.wav')
                evidence=decode_referenced_wave(path,destination,100000)
                self.assertEqual(evidence['sample_format'],'float32')
                self.assertEqual(destination.read_bytes(),raw);self.assertEqual(path.read_bytes(),raw)

    def test_truncated_float_payload_and_nonfinite_values_remain_rejected(self):
        for name,raw in [('truncated',floating_wave()[0][:-8]),('nan',floating_wave(invalid_sample=True)[0])]:
            with self.subTest(name=name):
                path=self.root/(name+'.wav');path.write_bytes(raw)
                with self.assertRaises(ValueError):assert_readable_wave_paths(self.document(path))


if __name__=='__main__':unittest.main()
