"""Exact, independently verified plaintext fingerprints for short PCM assets.

Only hashes and format facts are distributed, never the user's media. A profile
requires a complete encrypted-file match AND a complete plaintext/PCM match.
It is not an assertion that the original periodic parameter hash was solved.
Unknown inputs continue through the existing strict failure path.
"""
from __future__ import annotations

import hashlib
import io
import struct
import wave
from pathlib import Path

from .storage import sha

# Evidence: the preset input is byte-identical to an authored audioAlg asset;
# that asset has a separately preserved complete plaintext download witness.
# Both full WAVE bytes and every PCM byte were independently compared.
PROFILES = ({
    'source_sha256': 'dde0dce3049bfed08338ce846a26e147c4ad32dc7e8d8db5316b893ee23c424b',
    'plaintext_sha256': 'a67d663e061925a98eab43f32a781bae94072217c10cfdfac81c9463a5c15903',
    'pcm_sha256': '8c065b8c8daedb8923aba41da1a4ed7e8e249d8aefd1f3ba47187baaca4d518f',
    'body_size': 262222, 'key': 22, 'method': 3,
    'parameter_sha256': 'baca5c281235ac73e6e16490761b9e9d4a4f6944d1aeabb7a5d2dfd93afab129',
    'rate': 44100, 'frames': 65536, 'channels': 2, 'sample_width': 2,
},)


def verified_pcm_plan(source, body_size, key, method, commitment):
    candidates = [profile for profile in PROFILES
                  if (profile['body_size'], profile['key'], profile['method'], profile['parameter_sha256'])
                  == (body_size, key, method, commitment.hex())]
    if not candidates or body_size > 4 * 1024 * 1024:
        return None
    raw = Path(source).read_bytes()
    if len(raw) != body_size + 68:
        return None
    source_digest = hashlib.sha256(raw).hexdigest()
    profile = next((p for p in candidates if p['source_sha256'] == source_digest), None)
    if profile is None:
        return None
    plain = raw[:body_size].translate(bytes(i ^ key for i in range(256)))
    if (hashlib.sha256(plain).hexdigest() != profile['plaintext_sha256']
            or len(plain) < 44 or plain[:4] != b'RIFF' or plain[8:12] != b'WAVE'
            or struct.unpack('<I', plain[4:8])[0] + 8 != len(plain)):
        return None
    try:
        with wave.open(io.BytesIO(plain), 'rb') as audio:
            rate, count, channels, width = (audio.getframerate(), audio.getnframes(),
                                             audio.getnchannels(), audio.getsampwidth())
            if (audio.getcomptype() != 'NONE' or count <= 0 or count > 30 * rate
                    or (rate, count, channels, width) != (profile['rate'], profile['frames'],
                                                           profile['channels'], profile['sample_width'])):
                return None
            pcm = audio.readframes(count)
    except (wave.Error, EOFError):
        return None
    if len(pcm) != count * channels * width or hashlib.sha256(pcm).hexdigest() != profile['pcm_sha256']:
        return None
    if sha(source) != source_digest:
        raise ValueError('预设声音在完整指纹校验时改变，未采用兼容记录')
    return {'mode': 'full_xor', 'body_size': body_size, 'key': key, 'step': 0, 'length': body_size,
            'method': method, 'parameter_sha256': commitment.hex(),
            'parameter_commitment_verified': False, 'original_period_recovered': False,
            'validation': 'verified_complete_plaintext_fingerprint_v1', 'pcm_profile': dict(profile)}


def verify_profile_output(plan, source_digest, decoded, rate, frames, channels, width, pcm_digest):
    profile = plan.get('pcm_profile')
    if profile is None:
        return
    if (not any(profile == p for p in PROFILES)
            or plan.get('validation') != 'verified_complete_plaintext_fingerprint_v1'
            or source_digest != profile['source_sha256']
            or sha(decoded) != profile['plaintext_sha256'] or pcm_digest != profile['pcm_sha256']
            or (rate, frames, channels, width) != (profile['rate'], profile['frames'],
                                                    profile['channels'], profile['sample_width'])):
        raise ValueError('预设声音与已验证的完整明文指纹不一致，未发布解码文件')
