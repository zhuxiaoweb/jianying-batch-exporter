"""Strict BDVE WAVE plans for the user's explicitly referenced processed audio.

Recovered XOR parameters must match the file's SHA-256 parameter commitment.
Unrecognized files and unsolved plans fail closed; no amplitude-based repair.
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
import time
import io
import wave
from pathlib import Path

_VERIFIED_PARAMETERS = {}


def _complete_pcm_witness(source, body_size, key, solver_seconds=0):
    """An exact plaintext PCM witness for an otherwise unsolved short WAVE.

    The filename only proposes a candidate. Every decoded PCM byte, its format,
    complete frame count, exact RIFF sizes, and both stable input hashes decide
    acceptance. No original period-parameter commitment is claimed recovered.
    """
    source=Path(source).resolve(strict=True)
    if (body_size>4*1024*1024 or source.parent.name!='audioAlg'
            or source.parent.parent.name!='Resources' or not key):return None
    witness=source.with_name(source.name+'_download.wav')
    if not witness.is_file() or witness.is_symlink() or witness.resolve().parent!=source.parent:return None
    if witness.stat().st_size>4*1024*1024:return None
    raw=source.read_bytes();known=witness.read_bytes()
    if len(raw)!=body_size+68:return None
    decoded=raw[:body_size].translate(bytes(i^key for i in range(256)))
    witness_plan=None
    plaintext=known
    if known[:4]!=b'RIFF':
        # A download can itself be encrypted. Its plaintext is independent
        # evidence only after its own exact footer commitment has been solved.
        # Never chain witnesses or restart the caller's parameter-search budget.
        if solver_seconds<=0:return None
        try:witness_plan=wave_plan(witness,solver_seconds=solver_seconds,_allow_pcm_witness=False)
        except ValueError:return None
        if witness_plan.get('parameter_commitment_verified') is not True:return None
        plaintext=transform_block(known[:witness_plan['body_size']],0,witness_plan)
    def pcm(data):
        if len(data)<44 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE' or struct.unpack('<I',data[4:8])[0]+8!=len(data):
            raise ValueError('核对副本不是完整RIFF')
        with wave.open(io.BytesIO(data),'rb') as audio:
            params=audio.getparams();count=audio.getnframes()
            if (audio.getcomptype()!='NONE' or audio.getnchannels() not in (1,2)
                    or audio.getsampwidth() not in (2,3,4) or count<=0
                    or audio.getframerate()<=0 or count>30*audio.getframerate()):
                raise ValueError('核对副本不是已验证的短PCM')
            frames=audio.readframes(count)
            if len(frames)!=count*audio.getnchannels()*audio.getsampwidth():
                raise ValueError('核对副本PCM不完整')
            return params,frames
    try:params,actual=pcm(decoded);other_params,expected=pcm(plaintext)
    except (ValueError,wave.Error,EOFError):return None
    if params!=other_params or actual!=expected:return None
    fingerprint=hashlib.sha256(known).hexdigest()
    if hashlib.sha256(witness.read_bytes()).hexdigest()!=fingerprint or source.read_bytes()!=raw:
        raise ValueError('处理声音或明文核对副本在检查时改变')
    evidence={'path':str(witness.resolve()),'sha256':fingerprint,
        'pcm_sha256':hashlib.sha256(actual).hexdigest(),'frames':params.nframes,
        'channels':params.nchannels,'rate':params.framerate,'sample_width':params.sampwidth,
        'entire_pcm_equal':True,'source_unchanged_at_validation':True}
    if witness_plan is not None:
        evidence.update(encoding='parameter_committed_bdve',format_plan=witness_plan,
            plaintext_sha256=hashlib.sha256(plaintext).hexdigest())
    return {'mode':'full_xor','body_size':body_size,'key':key,'step':0,'length':body_size,
        'parameter_commitment_verified':False,'method':3,
        'parameter_sha256':raw[-44:-12].hex(),
        'validation':'complete_plaintext_pcm_witness_v1',
        'pcm_witness':evidence}


def _witness_boundary_candidates(source, body_size, key):
    """Propose exact source offsets from a complete independent plaintext PCM.

    These offsets are search hints only. They never establish a plan or replace
    the source footer's parameter commitment. A differently sized RIFF metadata
    area is permitted only when both complete PCM formats and frame counts match.
    """
    source = Path(source).resolve(strict=True)
    if (body_size > 4*1024*1024 or not key
            or source.parent.name != 'audioAlg'
            or source.parent.parent.name != 'Resources'):
        return [], []
    witness = source.with_name(source.name+'_download.wav')
    if (not witness.is_file() or witness.is_symlink()
            or witness.resolve().parent != source.parent
            or witness.stat().st_size > 4*1024*1024):
        return [], []
    raw = source.read_bytes()
    known = witness.read_bytes()
    if len(raw) != body_size+68:
        return [], []
    header_candidate = raw[:body_size].translate(bytes(i^key for i in range(256)))

    def complete_pcm(data):
        if (len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE'
                or struct.unpack_from('<I', data, 4)[0]+8 != len(data)):
            raise ValueError('candidate is not an exact RIFF')
        offset = 12
        pcm_offset = None
        while offset+8 <= len(data):
            size = struct.unpack_from('<I', data, offset+4)[0]
            end = offset+8+size
            if end > len(data):
                raise ValueError('candidate chunk is incomplete')
            if data[offset:offset+4] == b'data':
                if pcm_offset is not None:
                    raise ValueError('multiple PCM chunks')
                pcm_offset = offset+8
            offset = end+(size & 1)
        if offset != len(data) or pcm_offset is None:
            raise ValueError('candidate chunk layout is incomplete')
        with wave.open(io.BytesIO(data), 'rb') as audio:
            params = audio.getparams()
            if (params.comptype != 'NONE' or params.nchannels not in (1, 2)
                    or params.sampwidth not in (2, 3, 4) or params.framerate <= 0
                    or params.nframes <= 0 or params.nframes > 30*params.framerate):
                raise ValueError('candidate is not supported short PCM')
            frames = audio.readframes(params.nframes)
        if len(frames) != params.nframes*params.nchannels*params.sampwidth:
            raise ValueError('candidate PCM is incomplete')
        return params, frames, pcm_offset

    try:
        params, _, pcm_offset = complete_pcm(header_candidate)
        other_params, plaintext, _ = complete_pcm(known)
    except (ValueError, wave.Error, EOFError, struct.error):
        return [], []
    if params != other_params:
        return [], []
    encrypted_pcm = raw[pcm_offset:pcm_offset+len(plaintext)]
    mask = bytearray()
    for cipher, plain in zip(encrypted_pcm, plaintext):
        if cipher == plain:
            mask.append(0)
        elif cipher^key == plain:
            mask.append(1)
        else:
            return [], []
    if len(mask) != len(plaintext):
        return [], []
    ends, starts = [], []
    for index in range(1, len(mask)):
        if mask[index] != mask[index-1]:
            (starts if mask[index] else ends).append(pcm_offset+index)
    if source.read_bytes() != raw or witness.read_bytes() != known:
        raise ValueError('处理声音或明文核对副本在检查时改变')
    return ends, starts


def _parameter_hash(step, length, key):
    return hashlib.sha256(struct.pack('>iiB', step, length, key)).digest()


def _divisors(value):
    if value <= 0:
        return set()
    values = set()
    for n in range(1, math.isqrt(value)+1):
        if value % n == 0:
            values.update((n, value//n))
    return values


def _boundaries(source, body_size, key):
    # Long zero runs are candidate boundaries only. The parameter commitment,
    # rather than these observations, decides whether a candidate is accepted.
    on, off = bytes([key])*16, bytes(16)
    patterns = [re.compile(re.escape(on+off)), re.compile(re.escape(off+on))]
    found = [set(), set()]
    offset, carry = 0, b''
    with Path(source).open('rb') as stream:
        remaining = body_size
        while remaining:
            block = stream.read(min(1024*1024, remaining))
            if not block:
                raise ValueError('处理音频读取不完整')
            data = carry+block
            base = offset-len(carry)
            for pattern, values in zip(patterns, found):
                values.update(base+match.start()+16 for match in pattern.finditer(data))
            carry = data[-31:]
            offset += len(block)
            remaining -= len(block)
    return sorted(found[0]), sorted(found[1])


def _pcm_boundary_candidates(source, body_size, key):
    """Locate candidate changes in PCM high-byte distributions, never accept them.

    Optional NumPy accelerates first-use discovery. Exact footer hashes still
    decide correctness. Packaging this candidate requires this dependency.
    """
    try:
        import numpy as np
    except ImportError:
        return [], []
    if body_size > 128*1024*1024:
        return [], []
    with Path(source).open('rb') as stream:
        raw = np.frombuffer(stream.read(body_size), dtype=np.uint8)
    high = raw[1::2].astype(np.int16)
    alternate = np.bitwise_xor(high, key)
    cost = np.minimum(alternate, 256-alternate)-np.minimum(high, 256-high)
    width = 128
    if len(cost) <= width:
        return [], []
    cumulative = np.r_[0, np.cumsum(cost, dtype=np.int64)]
    encoded = (cumulative[width:]-cumulative[:-width]) < 0
    cuts = np.r_[0, np.where(encoded[1:] != encoded[:-1])[0]+1, len(encoded)]
    ends, starts = [], []
    for i in range(1, len(cuts)-1):
        # Short clear gaps can be only 145 samples wide. These are hints only;
        # the footer's exact parameter SHA still decides every accepted plan.
        if min(cuts[i]-cuts[i-1], cuts[i+1]-cuts[i]) >= 64:
            position = int((cuts[i]+width//2)*2+1)
            (starts if encoded[cuts[i]] else ends).append(position)
    return ends, starts


def _continuity_boundary_candidates(source, body_size, key):
    """Propose PCM boundaries when XOR preserves high-byte magnitudes.

    These are search hints, never an integrity check or a decoding decision.
    Every accepted parameter pair must match the footer's SHA-256 exactly.
    """
    try:import numpy as np
    except ImportError:return [], []
    if body_size>128*1024*1024:return [], []
    with Path(source).open('rb') as stream:raw=stream.read(body_size)
    raw=raw[:len(raw)//2*2]
    if len(raw)<8192:return [], []
    unsigned=np.frombuffer(raw,dtype='<u2')
    normal=unsigned.view('<i2').astype(np.int32)
    alternative=(unsigned^(key|(key<<8))).view('<i2').astype(np.int32)
    cost=np.abs(alternative[2:]-alternative[:-2])-np.abs(normal[2:]-normal[:-2])
    width=256
    cumulative=np.r_[0,np.cumsum(cost,dtype=np.int64)]
    mask=(cumulative[width:]-cumulative[:-width])<0
    cuts=np.r_[0,np.where(mask[1:]!=mask[:-1])[0]+1,len(mask)]
    ends,starts=[],[]
    for i in range(1,len(cuts)-1):
        if min(cuts[i]-cuts[i-1],cuts[i+1]-cuts[i])<512:continue
        center=int(cuts[i]+width//2)
        lo=max(0,center-width*2);hi=min(len(cost),center+width*2)
        local=np.r_[0,np.cumsum(cost[lo:hi],dtype=np.int64)]
        position=(lo+int(np.argmax(local) if mask[cuts[i]] else np.argmin(local)))*2
        (starts if mask[cuts[i]] else ends).append(position)
    return ends,starts


def _short_prefix_candidate(source, body_size, key):
    """A short first interval can be missed by the long-run PCM detector."""
    try:import numpy as np
    except ImportError:return None
    with Path(source).open('rb') as stream:raw=stream.read(min(body_size,16384))
    high=np.frombuffer(raw,dtype=np.uint8)[1::2].astype(np.int16)
    other=high^key
    cost=np.minimum(other,256-other)-np.minimum(high,256-high)
    width=8
    if len(cost)<=width:return None
    cumulative=np.r_[0,np.cumsum(cost,dtype=np.int64)]
    mask=(cumulative[width:]-cumulative[:-width])<0
    cuts=np.r_[0,np.where(mask[1:]!=mask[:-1])[0]+1,len(mask)]
    for i in range(1,len(cuts)-1):
        if mask[cuts[i]] or cuts[i]<8 or cuts[i+1]-cuts[i]<256:continue
        position=int((cuts[i]+width//2)*2+1)
        if position<=4096:return position
        break
    return None


def wave_plan(source, solver_seconds=10.0, *, _allow_pcm_witness=True):
    deadline=time.monotonic()+solver_seconds
    source = Path(source)
    size = source.stat().st_size
    with source.open('rb') as stream:
        header = stream.read(12)
        if size >= 68:
            stream.seek(-68, 2)
            footer = stream.read(68)
        else:
            footer = b''
    if len(header) != 12:
        raise ValueError('处理缓存没有完整音频头')
    key = header[0] ^ ord('R')
    decoded = bytes(value ^ key for value in header)
    if decoded[:4] != b'RIFF' or decoded[8:] != b'WAVE':
        raise ValueError('不是已识别的WAVE处理结果，不能猜测媒体格式')
    riff_size = struct.unpack('<I', decoded[4:8])[0]+8
    has_bdve = len(footer) == 68 and footer[4:8] == b'bdve'
    if not has_bdve:
        if key or riff_size != size:
            raise ValueError('缺少可验证的音频转换参数，拒绝整文件猜测转换')
        return {'mode': 'plain', 'body_size': size, 'key': 0, 'step': 0, 'length': 0,
                'parameter_commitment_verified': False}
    if (footer[:4] != struct.pack('>I', 68) or footer[8:12] != struct.pack('>I', 48)
            or footer[12:16] != b'crpt' or footer[16:20] != struct.pack('>I', 1)
            or footer[56:60] != struct.pack('>I', 12) or footer[60:64] != b'size'
            or footer[64:] != struct.pack('>I', 68) or riff_size != size-68):
        raise ValueError('处理音频封装结构尚未验证')
    method = struct.unpack('>I', footer[20:24])[0]
    commitment = footer[24:56]
    body_size = size-68

    def accepted(step, length):
        good=_parameter_hash(step,length,key)==commitment
        if good:_VERIFIED_PARAMETERS[(method,commitment,key)]=(step,length)
        return good

    def check_time():
        if time.monotonic()>deadline:raise ValueError('处理音频参数校验超时，保留源文件并停止')

    base = {'body_size': body_size, 'key': key, 'parameter_commitment_verified': True,
            'method': method, 'parameter_sha256': commitment.hex()}
    cached=_VERIFIED_PARAMETERS.get((method,commitment,key))
    if cached and accepted(*cached):
        step,length=cached
        mode={1:'full_xor',2:'prefix_xor',3:'periodic_xor'}.get(method)
        if mode:return {**base,'mode':mode,'step':step,'length':body_size if method==1 else length}
    if method == 1 and accepted(0, 0):
        return {**base, 'mode': 'full_xor', 'step': 0, 'length': body_size}
    if method not in (2, 3) or not key:
        raise ValueError('处理音频转换方式尚未验证')
    # A valid RIFF header and complete PCM shape do not identify which PCM
    # intervals were XOR-covered. Recover the committed parameters first.
    ends, starts = _boundaries(source, body_size, key)
    if method == 2:
        for length in ends:
            if accepted(0, length):
                return {**base, 'mode': 'prefix_xor', 'step': 0, 'length': length}
        raise ValueError('音频前缀参数未通过文件校验')
    gcd = 0
    for a, b in zip(ends, ends[1:]):
        gcd = math.gcd(gcd, b-a)
    for position in starts:
        gcd = math.gcd(gcd, position)
    for step in sorted(_divisors(gcd)):
        for end in ends:
            length = end % step
            if length >= 12 and accepted(step, length):
                return {**base, 'mode': 'periodic_xor', 'step': step, 'length': length}
    if _allow_pcm_witness:
        check_time()
        witnessed=_complete_pcm_witness(source,body_size,key,
            min(3.0,deadline-time.monotonic()))
        check_time()
        if witnessed:return witnessed
        # Independently preserved PCM can also expose exact interval boundaries.
        # These remain proposals: only this file's parameter SHA accepts a pair.
        witness_ends,_=_witness_boundary_candidates(source,body_size,key)
        check_time()
        for length in witness_ends[:4]:
            for step in range(max(13,length+1),2000001):
                if step%4096==0:check_time()
                if accepted(step,length):
                    return {**base,'mode':'periodic_xor','step':step,'length':length}
    approximate_ends, approximate_starts = _pcm_boundary_candidates(source, body_size, key)
    continuity_ends,continuity_starts=_continuity_boundary_candidates(source,body_size,key)
    # Try the independent continuity observations before broad one-dimensional
    # searches. Magnitude-only candidates can be displaced for keys near 255.
    for candidate_ends,candidate_starts in ((continuity_ends,continuity_starts),
                                           (approximate_ends,approximate_starts)):
        if not candidate_ends:continue
        centers=set(candidate_starts[:4])
        centers.update(b-a for a,b in zip(candidate_ends,candidate_ends[1:]))
        for radius in (8,128):
            check_time()
            for center in sorted(centers)[:8]:
                for step in range(max(13,center-radius),center+radius+1):
                    for end in candidate_ends[:2]:
                        length_center=end%step
                        for length in range(max(12,length_center-radius),min(step,length_center+radius+1)):
                            if accepted(step,length):return {**base,'mode':'periodic_xor','step':step,'length':length}
    prefix=_short_prefix_candidate(source,body_size,key)
    if prefix is not None:
        # The first interval may be only tens of bytes. It is still necessary
        # to recover the actual committed period; do not treat it as prefix-XOR.
        for delta in (-1,0,-2,1,-3,2,-4,3,-5):
            length=prefix+delta
            if length<12:continue
            for step in range(length+1,2000001):
                if step%4096==0:check_time()
                if accepted(step,length):return {**base,'mode':'periodic_xor','step':step,'length':length}
    if approximate_ends:
        # A short file can contain only one interval even when step exceeds its
        # length. Check a bounded parameter space, never invent an equivalent
        # but uncommitted step. Most such files resolve using the first candidate.
        for delta in (-1, -2, 0, -3, 1, -4, 2, -5, 3):
            length = approximate_ends[0]+delta
            if length < 12:
                continue
            for step in range(length+1, 2000001):
                if step % 4096 == 0 and time.monotonic() > deadline:
                    raise ValueError('处理音频参数校验超时，保留源文件并停止')
                if accepted(step, length):
                    return {**base, 'mode': 'periodic_xor', 'step': step, 'length': length}
    # Short clips may expose only the first encrypted interval. A bounded
    # one-dimensional search is allowed, and still requires an exact hash match.
    for length in ends[:4]:
        for step in range(length+1, min(body_size, 2147483647)+1):
            if step % 4096 == 0 and time.monotonic() > deadline:
                raise ValueError('处理音频参数校验超时，保留源文件并停止')
            if accepted(step, length):
                return {**base, 'mode': 'periodic_xor', 'step': step, 'length': length}
    if _allow_pcm_witness:
        witnessed=_complete_pcm_witness(source,body_size,key)
        if witnessed:return witnessed
    from .verified_pcm import verified_pcm_plan
    verified=verified_pcm_plan(source,body_size,key,method,commitment)
    if verified:return verified
    raise ValueError('音频分段参数未通过文件校验，拒绝猜测转换')


def transform_block(block, offset, plan):
    if plan['mode'] == 'plain':
        return block
    table = bytes(value ^ plan['key'] for value in range(256))
    if plan['mode'] == 'full_xor':
        return block.translate(table)
    if plan['mode'] == 'prefix_xor':
        length = max(0, min(len(block), plan['length']-offset))
        return block[:length].translate(table)+block[length:]
    output = bytearray(block)
    step, length = plan['step'], plan['length']
    for start in range((offset//step)*step, offset+len(block), step):
        left, right = max(offset, start)-offset, min(offset+len(block), start+length)-offset
        if right > left:
            output[left:right] = block[left:right].translate(table)
    return bytes(output)
