"""Real codec and fail-closed checks for paired project ADTS cache copies."""
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from jyexport.encoded_audio import (adts_info, bind_referenced_aac,
    decode_referenced_aac, has_footer)
from jyexport.advanced import prepare_resources
from jyexport.storage import Cancelled, sha

APP = Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')


@unittest.skipUnless((APP/'ffmpeg.exe').is_file(), 'real AAC encoder unavailable')
class EncodedAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        target = Path(cls.assets.name)/'plain.aac'
        p = subprocess.run([str(APP/'ffmpeg.exe'), '-nostdin', '-v', 'error',
            '-f', 'lavfi', '-i', 'sine=frequency=523:sample_rate=44100:duration=2',
            '-ac', '2', '-c:a', 'aac', '-b:a', '192k', '-f', 'adts', str(target)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert p.returncode == 0, p.stderr
        cls.body = target.read_bytes()

    @classmethod
    def tearDownClass(cls): cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.project = self.root/'backup'
        self.resources = self.project/'Resources/combination'; self.resources.mkdir(parents=True)
        self.source = self.resources/'first.aac'; self.witness = self.resources/'second.aac'
        self.source.write_bytes(self.wrap(self.body,156)); self.witness.write_bytes(self.wrap(self.body,222))
        self.output = self.root/'work/readable.aac'
        self.document = {'tracks':[{'id':'audio','type':'audio','segments':[
            {'id':'seg','material_id':'m','source_timerange':{'start':0,'duration':2000000},
             'target_timerange':{'start':1000000,'duration':2000000},'volume':.42}]}],
            'materials':{'audios':[{'id':'m','type':'music','name':'preset',
                'duration':2000000,'path':str(self.source)}]}}

    @staticmethod
    def wrap(body,key):
        commitment=hashlib.sha256(struct.pack('>iiB',1000003,800011,key)).digest()
        return (body.translate(bytes(i^key for i in range(256))) + struct.pack('>I',68)
            + b'bdve' + struct.pack('>I',48) + b'crpt' + struct.pack('>II',1,3)
            + commitment + struct.pack('>I',12) + b'size' + struct.pack('>I',68))

    def decode(self,**kw):
        args=dict(source=self.source,destination=self.output,project_folder=self.project,
                  app_directory=APP,expected_duration_us=2000000)
        args.update(kw); return decode_referenced_aac(**args)

    def test_real_codec_encoded_bytes_sources_and_tracks_are_preserved(self):
        before={p:sha(p) for p in (self.source,self.witness)}
        authored=copy.deepcopy(self.document)
        prepared,evidence=bind_referenced_aac(self.document,self.project,self.output.parent,APP)
        item=evidence['bindings'][0]['audio']
        self.assertEqual(Path(item['decoded']).read_bytes(),self.body)
        self.assertTrue(item['complete_decode']); self.assertFalse(item['parameter_commitment_verified'])
        self.assertEqual(before,{p:sha(p) for p in before})
        self.assertEqual(self.document,authored); self.assertEqual(prepared['tracks'],authored['tracks'])
        self.assertTrue(has_footer(self.source)); self.assertFalse(has_footer(item['decoded']))

    def test_no_independent_witness_fails(self):
        self.witness.unlink()
        with self.assertRaisesRegex(ValueError,'独立核对'):self.decode()
        self.assertFalse(self.output.exists())

    def test_same_key_or_different_content_is_not_a_witness(self):
        for body,key in [(self.body,156),(self.body[:-1]+b'X',222)]:
            self.witness.write_bytes(self.wrap(body,key))
            with self.assertRaisesRegex(ValueError,'独立核对'):self.decode()

    def test_malformed_footer_and_truncated_adts_fail(self):
        r=bytearray(self.source.read_bytes());r[-1]^=1;self.source.write_bytes(r)
        with self.assertRaisesRegex(ValueError,'封装'):self.decode()
        self.source.write_bytes(self.wrap(self.body[:-1],156))
        with self.assertRaisesRegex(ValueError,'帧不完整'):self.decode()

    def test_shortened_whole_frames_cannot_satisfy_authored_duration(self):
        first=((self.body[3]&3)<<11)|(self.body[4]<<3)|(self.body[5]>>5)
        short=self.body[:first]
        self.source.write_bytes(self.wrap(short,156));self.witness.write_bytes(self.wrap(short,222))
        with self.assertRaises(ValueError):self.decode()

    def test_complete_decoder_errors_wrong_count_and_nonfinite_samples_fail(self):
        from types import SimpleNamespace
        info=adts_info(self.body);size=info['samples']*info['channels']
        for result in [SimpleNamespace(returncode=1,stderr=b'bad',stdout=b''),
                SimpleNamespace(returncode=0,stderr=b'',stdout=b'\0'*4),
                SimpleNamespace(returncode=0,stderr=b'',stdout=struct.pack('<f',float('nan'))*size)]:
            with patch('jyexport.encoded_audio.subprocess.run',return_value=result):
                with self.assertRaises(ValueError):self.decode()
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.output.parent.glob('*.partial-*')))

    def test_source_or_witness_change_during_decode_is_rejected(self):
        real=subprocess.run
        for changed in (self.source,self.witness):
            self.source.write_bytes(self.wrap(self.body,156));self.witness.write_bytes(self.wrap(self.body,222))
            def mutate(*a,**kw):
                result=real(*a,**kw);changed.write_bytes(changed.read_bytes()+b'x');return result
            with patch('jyexport.encoded_audio.subprocess.run',side_effect=mutate):
                with self.assertRaisesRegex(ValueError,'读取时改变'):self.decode()
            self.assertFalse(self.output.exists())

    def test_source_range_and_ownership_are_strict(self):
        self.document['tracks'][0]['segments'][0]['source_timerange']['start']=3000000
        with self.assertRaisesRegex(ValueError,'源范围'):
            bind_referenced_aac(self.document,self.project,self.output.parent,APP)
        external=self.root/'external.aac';external.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError,'原草稿内'):self.decode(source=external)

    def test_each_repeated_material_reference_is_checked_and_reuses_one_copy(self):
        segments=self.document['tracks'][0]['segments']
        second=copy.deepcopy(segments[0]);second['id']='second';segments.append(second)
        with patch('jyexport.encoded_audio.decode_referenced_aac',wraps=decode_referenced_aac) as decode:
            prepared,evidence=bind_referenced_aac(self.document,self.project,self.root/'good',APP)
        self.assertEqual(decode.call_count,1);self.assertEqual(len(evidence['bindings']),2)
        self.assertEqual(prepared['tracks'],self.document['tracks'])
        second['source_timerange']['start']=3000000
        with self.assertRaisesRegex(ValueError,'源范围'):
            bind_referenced_aac(self.document,self.project,self.root/'bad',APP)

    def test_cancel_and_existing_output_are_preserved(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):self.decode(cancel=stop)
        self.output.parent.mkdir();self.output.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):self.decode()
        self.assertEqual(self.output.read_bytes(),b'existing')

    def test_preparation_resume_checks_every_source_and_witness(self):
        work=self.root/'run';work.mkdir()
        first,record=prepare_resources(self.document,self.project,work,APP)
        with patch('jyexport.encoded_audio.subprocess.run',side_effect=AssertionError('must reuse')):
            second,again=prepare_resources(self.document,self.project,work,APP)
        self.assertEqual(first,second);self.assertEqual(record,again)
        self.witness.write_bytes(self.witness.read_bytes()+b'x')
        with self.assertRaisesRegex(ValueError,'核对副本'):
            prepare_resources(self.document,self.project,work,APP)

    def test_old_preparation_can_migrate_without_removing_old_payload(self):
        work=self.root/'run';work.mkdir()
        # A legacy preparation pointed directly to the still-encrypted AAC.
        from jyexport.advanced import REVISION,digest
        from jyexport.media_inputs import required_media_snapshot
        from jyexport.storage import atomic_json
        payload=work/'legacy.json';atomic_json(payload,self.document)
        record={'revision':REVISION,'authored_sha256':digest(self.document),
            'prepared_document':str(payload),'prepared_sha256':sha(payload),
            'processing':{'bindings':[]},'media_results':{'bindings':[]},
            'required_media':required_media_snapshot(self.document)}
        atomic_json(work/'advanced-preparation.json',record)
        prepared,migrated=prepare_resources(self.document,self.project,work,APP)
        self.assertTrue(payload.exists());self.assertEqual(json.loads(payload.read_text('utf-8')),self.document)
        self.assertNotEqual(migrated['prepared_document'],str(payload))
        self.assertEqual(prepared['tracks'],self.document['tracks'])


if __name__=='__main__':unittest.main()
