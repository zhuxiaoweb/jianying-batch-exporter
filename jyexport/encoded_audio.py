"""Read a narrow, independently corroborated BDVE ADTS cache format.

No periodic WAVE commitment rule is relaxed. For short, fully XOR-covered
ADTS files whose original period is not observable, require a second project
copy with a different key/footer and byte-identical recovered encoded content.
Both complete ADTS grammar and full AAC decoding must pass. This is a separate
format verification contract, not a claim to have recovered the period hash.
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import struct
import subprocess
import uuid

from .draft import materials
from .fast import validate_output_root
from .storage import Cancelled, sha
from .timeline_graph import documents

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 30
RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
         16000, 12000, 11025, 8000, 7350)


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled('预设声音检查已停止')


def has_footer(path):
    path = Path(path)
    if path.suffix.casefold() != '.aac' or path.stat().st_size < 68:
        return False
    with path.open('rb') as stream:
        stream.seek(-64, 2)
        return stream.read(4) == b'bdve'


def _candidate(path):
    raw = Path(path).read_bytes()
    if not 82 <= len(raw) <= MAX_BYTES:
        raise ValueError('加密预设AAC超出已验证的短音效范围')
    footer = raw[-68:]
    if (footer[:24] != struct.pack('>I', 68) + b'bdve' + struct.pack('>I', 48)
            + b'crpt' + struct.pack('>II', 1, 3)
            or footer[56:] != struct.pack('>I', 12) + b'size' + struct.pack('>I', 68)):
        raise ValueError('加密预设AAC封装尚未支持')
    key = raw[0] ^ 255
    if not key:
        raise ValueError('加密预设AAC没有可验证的独立转换键')
    body = raw[:-68].translate(bytes(i ^ key for i in range(256)))
    info = adts_info(body)
    return raw, body, key, info


def adts_info(body):
    offset = count = 0
    profile = None
    while offset < len(body):
        h = body[offset:offset + 7]
        if len(h) != 7 or h[0] != 255 or h[1] != 241:
            raise ValueError('预设AAC不是完整一致的ADTS编码帧')
        index = (h[2] >> 2) & 15
        channels = ((h[2] & 1) << 2) | (h[3] >> 6)
        current = ((h[2] >> 6) + 1, index, channels)
        if current[0] != 2 or index >= len(RATES) or channels not in (1, 2) or h[6] & 3:
            raise ValueError('预设AAC编码参数超出已验证范围')
        if profile is not None and profile != current:
            raise ValueError('预设AAC的编码参数在文件中发生变化')
        profile = current
        length = ((h[3] & 3) << 11) | (h[4] << 3) | (h[5] >> 5)
        if length < 8 or offset + length > len(body):
            raise ValueError('预设AAC编码帧不完整')
        offset += length
        count += 1
    if count < 2 or count * 1024 > MAX_SECONDS * RATES[profile[1]]:
        raise ValueError('预设AAC没有完整的短音效编码内容')
    return {'rate': RATES[profile[1]], 'channels': profile[2],
            'frames': count, 'samples': count * 1024, 'encoded_bytes': offset}


def decode_referenced_aac(source, destination, project_folder, app_directory,
                          expected_duration_us, cancel=None):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    resource = (Path(project_folder).resolve(strict=True) / 'Resources' / 'combination').resolve(strict=True)
    if source.parent != resource or source.is_symlink():
        raise ValueError('加密AAC尚只支持原草稿内的预设组合音效')
    if destination.exists() or destination == source:
        raise FileExistsError('预设声音输出已存在，保留原有文件')
    validate_output_root(destination.parent)
    if not 0 < expected_duration_us <= MAX_SECONDS * 1000000:
        raise ValueError('预设AAC缺少已验证的短音效时长')
    _cancelled(cancel)
    raw, body, key, info = _candidate(source)
    before = hashlib.sha256(raw).hexdigest()
    payload_hash = hashlib.sha256(body).hexdigest()
    # Corroboration reads another backup asset, never substitutes its sound.
    # A constant nonzero XOR relation across every ciphertext byte, distinct
    # keys/commitments, identical full encoded bytes and complete codec checks
    # are all required. A lone plausible header is never enough.
    witness = None
    candidates = sorted(resource.glob('*.aac'))
    if len(candidates) > 256:
        raise ValueError('预设音效副本过多，停止自动核对')
    for other in candidates:
        _cancelled(cancel)
        if (other.resolve() == source or other.is_symlink() or other.resolve().parent != resource
                or other.stat().st_size != len(raw) or not has_footer(other)):
            continue
        try:
            other_raw, other_body, other_key, other_info = _candidate(other)
        except ValueError:
            continue
        if (other_key != key and other_raw[-44:-12] != raw[-44:-12]
                and other_body == body and other_info == info):
            witness = {'source': str(other.resolve()),
                       'source_sha256': hashlib.sha256(other_raw).hexdigest(),
                       'decoded_payload_sha256': payload_hash,
                       'different_key_and_commitment': True}
            break
    if witness is None:
        raise ValueError('此加密预设音效尚无可独立核对的完整副本，已停止以免丢失声音：' + source.name)
    if expected_duration_us / 1e6 > info['samples'] / info['rate']:
        raise ValueError('预设AAC的完整采样不能覆盖草稿时长')
    if abs(expected_duration_us / 1e6 - info['samples'] / info['rate']) > .05:
        raise ValueError('预设AAC的完整采样与草稿时长不符')
    tool = Path(app_directory or '') / 'ffmpeg.exe'
    if not tool.is_file():
        raise FileNotFoundError('缺少ffmpeg.exe，不能完整核验预设声音')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.partial-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        _cancelled(cancel)
        decoded = subprocess.run([str(tool), '-nostdin', '-v', 'error', '-xerror',
            '-f', 'aac', '-i', str(temporary), '-map', '0:a:0', '-c:a', 'pcm_f32le',
            '-f', 'f32le', '-'], stdin=subprocess.DEVNULL, capture_output=True,
            timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _cancelled(cancel)
        if decoded.returncode or decoded.stderr or len(decoded.stdout) != info['samples'] * info['channels'] * 4:
            raise ValueError('预设AAC完整解码或采样数量核验失败')
        if any(not math.isfinite(value[0]) for value in struct.iter_unpack('<f', decoded.stdout)):
            raise ValueError('预设AAC包含无效音频采样')
        if (sha(source) != before or sha(witness['source']) != witness['source_sha256']
                or sha(temporary) != payload_hash):
            raise ValueError('预设声音来源、副本或工作文件读取时改变，未发布')
        os.rename(temporary, destination)
        return {'source': str(source), 'source_sha256': before, 'decoded': str(destination),
                'decoded_sha256': payload_hash, 'source_unchanged': True,
                'witness': witness, 'codec': 'aac_lc_adts', **info,
                'complete_decode': True, 'pcm_sha256': hashlib.sha256(decoded.stdout).hexdigest(),
                'parameter_commitment_verified': False,
                'validation': 'independent_ciphertext_relation_complete_adts_and_decode_v1',
                'timeline_unchanged': True, 'render_equivalence_verified': False}
    finally:
        if temporary.exists():
            temporary.unlink()


def bind_referenced_aac(data, project_folder, output_directory, app_directory=None, cancel=None):
    result = copy.deepcopy(data)
    converted, bindings = {}, []
    for scope, document in documents(result):
        lookup = materials(document)
        instances = {}
        for track in document['tracks']:
            if track.get('type') != 'audio':
                continue
            for segment in track.get('segments', []):
                instances.setdefault(segment.get('material_id'), []).append(segment)
        for mid, segments in instances.items():
            material = lookup.get(mid, {})
            value = material.get('path')
            if (material.get('type') not in ('music', 'sound', 'sound_effect', 'extract_music')
                    or not value or not has_footer(value)):
                continue
            duration = int(material.get('duration', 0))
            key = (str(Path(value).resolve(strict=True)), duration)
            if key not in converted:
                converted[key] = decode_referenced_aac(value,
                    Path(output_directory) / (uuid.uuid4().hex + '.aac'), project_folder,
                    app_directory, duration, cancel)
            audio = converted[key]
            for segment in segments:
                interval = segment.get('source_timerange') or {}
                start, length = int(interval.get('start', 0)), int(interval.get('duration', 0))
                if start < 0 or length <= 0 or (start + length) * audio['rate'] > audio['samples'] * 1000000:
                    raise ValueError('预设AAC不能完整覆盖片段源范围：' + str(segment.get('id')))
                bindings.append({'scope': list(scope), 'material_id': material['id'],
                                 'segment_id': segment['id'], 'audio': audio})
            material['path'] = audio['decoded']
    return result, {'bindings': bindings, 'decoded_files': len(converted), 'timeline_unchanged': True}
