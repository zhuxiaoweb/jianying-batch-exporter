"""Validate authored 32-bit float WAVE without transcoding its samples.

Python's wave reader intentionally accepts integer PCM only. A normal IEEE
float WAVE must not be mistaken for an encrypted or broken audio asset.
"""
from __future__ import annotations
import hashlib
import math
import struct
from pathlib import Path

FLOAT_GUID=bytes.fromhex('0300000000001000800000aa00389b71')


def inspect_float_wave(path):
    path=Path(path)
    size=path.stat().st_size
    fmt=None;data=None
    with path.open('rb') as stream:
        header=stream.read(12)
        if len(header)!=12 or header[:4]!=b'RIFF' or header[8:]!=b'WAVE':
            raise ValueError('不是RIFF/WAVE文件')
        declared=struct.unpack('<I',header[4:8])[0]+8
        if declared!=size:raise ValueError('WAVE文件长度与声明不符，数据可能不完整')
        offset=12
        while offset<declared:
            stream.seek(offset);chunk=stream.read(8)
            if len(chunk)!=8:raise ValueError('WAVE块头不完整')
            name=chunk[:4];length=struct.unpack('<I',chunk[4:])[0]
            first=offset+8;last=first+length
            if last>declared:raise ValueError('WAVE采样数据不完整')
            if name==b'fmt ':
                if fmt is not None or length<16 or length>4096:raise ValueError('WAVE格式块不明确')
                payload=stream.read(length)
                code,channels,rate,byte_rate,align,bits=struct.unpack('<HHIIHH',payload[:16])
                if code==65534:
                    if length<40 or struct.unpack('<H',payload[16:18])[0]<22 or payload[24:40]!=FLOAT_GUID:
                        raise ValueError('不是已支持的浮点WAVE子格式')
                elif code!=3:raise ValueError('不是IEEE浮点WAVE')
                if channels not in (1,2) or rate<=0 or bits!=32 or align!=channels*4 or byte_rate!=rate*align:
                    raise ValueError('浮点WAVE参数尚未支持或不一致')
                fmt={'channels':channels,'rate':rate,'sample_width':4,'sample_format':'float32','block_align':align}
            elif name==b'data':
                if data is not None:raise ValueError('多个采样块尚未支持')
                data=(first,length)
            offset=last+(length&1)
        if offset!=declared or fmt is None or data is None:raise ValueError('WAVE结构不完整')
        first,length=data
        if not length or length%fmt['block_align']:raise ValueError('WAVE采样数据不完整')
        digest=hashlib.sha256();stream.seek(first);remaining=length
        while remaining:
            chunk=stream.read(min(1024*1024,remaining))
            if not chunk:raise ValueError('WAVE采样数据不完整')
            if any(not math.isfinite(value[0]) for value in struct.iter_unpack('<f',chunk)):
                raise ValueError('浮点WAVE含无效采样值')
            digest.update(chunk);remaining-=len(chunk)
    return {**fmt,'frames':length//fmt['block_align'],'data_offset':first,'data_size':length,
            'pcm_sha256':digest.hexdigest()}
