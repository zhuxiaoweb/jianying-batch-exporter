"""Post-render audio safety checks.

The standalone JianYing renderer can produce a valid MP4 whose decoded mix is
already above 0 dBFS. Input decoding and authored mixing both need investigation.
Detect it before publishing a four-version archive and retain the masters.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


_LEVEL_VALUE = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?|[-+]?(?:inf|nan))(?=\s|$)"
_PEAK_LINE = re.compile(r"Peak level dB:\s*" + _LEVEL_VALUE, re.I)
_RMS_LINE = re.compile(r"RMS level dB:\s*" + _LEVEL_VALUE, re.I)
MAX_SAFE_PEAK_DB = 0.5


def has_safe_audio_diagnostics(record):
    """Return whether a completed render record carries a current safe gate."""
    renders = record.get('renders') if isinstance(record, dict) else None
    if not isinstance(renders, dict):
        return False
    for key in ('V2', 'V3'):
        render = renders.get(key)
        if not isinstance(render, dict):
            return False
        diagnostics = render.get('audio_diagnostics')
        if not isinstance(diagnostics, dict):
            return False
        if diagnostics.get('method') != 'ffmpeg_astats_full_decode_v1':
            return False
        if diagnostics.get('safe') is not True:
            return False
        peak=diagnostics.get('peak_dbfs');rms=diagnostics.get('rms_dbfs')
        counts=('sample_count','nan_samples','inf_samples')
        has_counts=any(name in diagnostics for name in counts)
        if has_counts and (any(type(diagnostics.get(name)) is not int for name in counts) or
                           diagnostics['sample_count']<=0 or diagnostics['nan_samples']!=0 or
                           diagnostics['inf_samples']!=0):
            return False
        if peak=='-inf' and rms=='-inf':
            if diagnostics.get('digital_silence') is not True or not has_counts:return False
            continue
        try:
            finite=(type(peak) in (int,float) and type(rms) in (int,float) and
                    math.isfinite(peak) and math.isfinite(rms))
        except OverflowError:
            return False
        if (not finite or diagnostics.get('digital_silence') is True or peak>MAX_SAFE_PEAK_DB):
            return False
    return True


def audio_peak_diagnostics(path, app_dir):
    """Return the decoded full-mix peak and RMS in dBFS.

    ``astats`` is used only for diagnosis.  It does not write the input or
    alter the audio stream.  The full stream is decoded so a short clean
    prefix cannot hide a later clipped section.

    Paired negative-infinity levels represent verified digital silence and
    are recorded as strings so receipts remain standard JSON.
    """
    source = Path(path).resolve(strict=True)
    ffmpeg = Path(app_dir).resolve() / 'ffmpeg.exe'
    if not ffmpeg.is_file():
        raise FileNotFoundError('缺少ffmpeg.exe，无法检查成片音频峰值')
    # astats omits NaN/Inf counters for integer formats. Float conversion makes
    # those observations explicit without clipping or writing media.
    command = [str(ffmpeg), '-nostdin', '-hide_banner', '-loglevel', 'info',
               '-i', str(source), '-map', '0:a:0',
               '-af', 'aformat=sample_fmts=flt,astats=metadata=0:reset=0:measure_perchannel=none:'
                     'measure_overall=Peak_level+RMS_level+Number_of_samples+'
                     'Number_of_NaNs+Number_of_Infs',
               '-f', 'null', '-']
    result = subprocess.run(command, capture_output=True, timeout=3600,
                            encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('成片音频峰值检查失败：' + result.stderr[-1200:])
    lines = result.stderr.splitlines()
    overall=[index for index,line in enumerate(lines) if 'Overall' in line]
    section='\n'.join(lines[overall[-1]+1:]) if overall else ''
    peak_match=_PEAK_LINE.search(section);rms_match=_RMS_LINE.search(section)
    counts={}
    for field,label in (('sample_count','samples'),('nan_samples','NaNs'),('inf_samples','Infs')):
        match=re.search(r'Number of '+label+r':\s*'+_LEVEL_VALUE,section,re.I)
        if match:
            value=float(match.group(1))
            if math.isfinite(value) and value>=0 and value.is_integer():counts[field]=int(value)
    if not peak_match or not rms_match or len(counts)!=3 or counts['sample_count']<=0:
        raise ValueError('成片音频峰值检查未返回有效结果（缺少完整Peak/RMS/样本检查）')
    peak=float(peak_match.group(1));rms=float(rms_match.group(1))
    if counts['nan_samples'] or counts['inf_samples']:
        raise ValueError('成片音频包含NaN或Inf无效采样，已停止本集归档')
    silence=peak==float('-inf') and rms==float('-inf')
    if not silence and (not math.isfinite(peak) or not math.isfinite(rms)):
        raise ValueError('成片音频峰值检查包含NaN或Inf无效数值，已停止本集归档')
    return {'peak_dbfs':'-inf' if silence else peak,'rms_dbfs':'-inf' if silence else rms,
            'threshold_dbfs':MAX_SAFE_PEAK_DB,'digital_silence':silence,**counts,
            'safe':silence or peak<=MAX_SAFE_PEAK_DB,'method':'ffmpeg_astats_full_decode_v1'}


def assert_audio_safe(path, app_dir, label):
    """Raise a user-facing error when the rendered full mix is overrange."""
    diagnostics = audio_peak_diagnostics(path, app_dir)
    if diagnostics['safe']:
        return diagnostics
    peak = diagnostics['peak_dbfs']
    raise ValueError(
        f'{label}母版音频疑似削波：解码峰值 {peak:+.2f} dBFS，已停止本集归档，'
        '原稿和母版均保留。请查看本集诊断，核对处理声音的解码依据和混音结果。'
        '可在剪映专业版原生导出同集进行对照。'
    )
