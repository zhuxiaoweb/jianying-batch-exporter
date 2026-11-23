"""Portable synthetic render acceptance; never reads a user's draft library."""
from __future__ import annotations

import copy
import hashlib
import json
import platform
import struct
import subprocess
import sys
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .audio_diagnostics import assert_audio_safe, audio_peak_diagnostics
from .engine import EngineAdapter, meaningful_changes, render_projection
from .fast import compose_bundle, validate_output_root
from .media_inputs import required_media_snapshot
from .processed_audio import bind_referenced_media_results, bind_referenced_results
from .scoped_variants import inspect, subtitle_srt, transform
from .storage import Cancelled, atomic_json, now, sha
from .timeline_graph import documents, local_references

RATE = 44100
DURATION = 12000000


def _run(app, arguments, timeout=120):
    result = subprocess.run([str(Path(app)/'ffmpeg.exe'), '-nostdin', '-v', 'error', *arguments],
        capture_output=True, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(result.stderr.decode('utf-8', 'replace')[-1600:])
    return result.stdout


def _tone(path, seconds, frequency, amplitude, silence=0):
    import numpy as np
    ticks = np.arange(round(seconds*RATE))/RATE
    signal = amplitude*np.sin(2*np.pi*frequency*ticks)
    signal[ticks < silence] = 0
    signal *= np.minimum(1, np.maximum(0, (seconds-ticks)/.03))
    pcm = np.round(signal*32767).astype('<i2')
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
        stream.writeframes(np.repeat(pcm, 2).tobytes())
    return pcm.astype(float)/32768


def _wrap_periodic(path, key, step, length):
    # Synthetic format fixture authored here. It contains no third-party media.
    body = bytearray(Path(path).read_bytes())
    expected_sha = hashlib.sha256(body).hexdigest()
    for start in range(0, len(body), step):
        limit = min(start+length, len(body))
        body[start:limit] = bytes(value ^ key for value in body[start:limit])
    commitment = hashlib.sha256(struct.pack('>iiB', step, length, key)).digest()
    footer = (struct.pack('>I', 68)+b'bdve'+struct.pack('>I', 48)+b'crpt'+
        struct.pack('>II', 1, 3)+commitment+struct.pack('>I', 12)+b'size'+struct.pack('>I', 68))
    Path(path).write_bytes(bytes(body)+footer)
    return expected_sha


def _segment(identifier, material, start, duration, source_start=0, refs=()):
    return {'id': identifier, 'material_id': material,
        'target_timerange': {'start': start, 'duration': duration},
        'source_timerange': {'start': source_start, 'duration': duration},
        'extra_material_refs': list(refs), 'volume': 1.0}


def fixture(directory, app_directory):
    """Return authored document, known roles and independent PCM oracle."""
    import numpy as np
    directory = Path(directory)
    audio = directory/'Resources'/'audioAlg'
    audio.mkdir(parents=True)
    _run(app_directory, ['-n', '-f', 'lavfi', '-i', 'testsrc2=size=270x480:rate=60',
        '-t', '12', '-an', '-c:v', 'mpeg4', '-q:v', '2', '-pix_fmt', 'yuv420p',
        str(directory/'picture.mp4')])
    voice = _tone(audio/'voice.wav', 10, 440, .09, silence=.5)
    environment = _tone(directory/'environment.wav', 10, 660, .025)
    music = _tone(directory/'music.wav', 12, 220, .025)
    short = _tone(directory/'short.wav', .4, 330, .04)
    compound = _tone(directory/'compound.wav', 2, 1100, .05)
    _tone(directory/'processing-input.wav', 8, 1320, .03)
    processing = _tone(audio/'processing-output.wav', 4, 770, .04, silence=.7)
    expected_files = {
        str(audio/'voice.wav'): _wrap_periodic(audio/'voice.wav', 78, 196611, 65539),
        str(audio/'processing-output.wav'): _wrap_periodic(audio/'processing-output.wav', 43, 262147, 88013)}

    def audio_mat(identifier, path, duration):
        return {'id': identifier, 'type': 'extract_music', 'name': identifier,
                'path': str(path), 'duration': duration}
    tracks = [
        {'id': 'picture', 'type': 'video', 'segments': [_segment('picture-s', 'picture-m', 0, DURATION)]},
        {'id': 'voice', 'type': 'audio', 'name': 'VOICE 1', 'segments': [_segment('voice-s', 'voice-m', 0, 10000000)]},
        {'id': 'environment', 'type': 'audio', 'name': 'AMBIENCE 1', 'segments': [_segment('environment-s', 'environment-m', 0, 10000000)]},
        {'id': 'music', 'type': 'audio', 'name': 'BGM 1', 'segments': [_segment('music-s', 'music-m', 0, DURATION)]},
        {'id': 'short', 'type': 'audio', 'name': 'SFX 1', 'segments': [_segment('short-s', 'short-m', 4000000, 400000)]},
        {'id': 'processed', 'type': 'audio', 'name': 'AMBIENCE 2', 'segments': [_segment('processed-s', 'processed-m', 6000000, 1000000, 3500000, ['processing-node'])]},
        {'id': 'compound', 'type': 'audio', 'name': 'SFX 2', 'segments': [_segment('compound-s', 'compound-shell', 10000000, 2000000, refs=['compound-m'])]},
    ]
    tracks[0]['segments'][0].update(volume=0, clip={'scale': {'x': 1., 'y': 1.}, 'transform': {'x': 0., 'y': 0.}})
    child = {'id': str(uuid.uuid4()), 'new_version': '189.0.0', 'duration': 2000000, 'fps': 24.,
        'canvas_config': {'width': 1080, 'height': 1920, 'ratio': '9:16'},
        'tracks': [{'id': 'child-audio', 'type': 'audio', 'name': 'SFX 1',
                    'segments': [_segment('child-s', 'child-m', 0, 2000000)]}],
        'materials': {'audios': [audio_mat('child-m', directory/'compound.wav', 2000000)]}}
    font = Path(__import__('os').environ.get('SystemRoot', 'C:/Windows'))/'Fonts'/'arial.ttf'
    if not font.is_file():
        raise ValueError('合成字幕测试缺少Windows Arial字体')
    text = 'TEST CAPTIONS'
    content = json.dumps({'text': text, 'styles': [{'range': [0, len(text)], 'size': 16.,
        'font': {'path': str(font)}, 'fill': {'content': {'solid': {'color': [1., 1., 1.]}}}}]})
    tracks.append({'id': 'caption', 'type': 'text', 'segments': [
        {'id': 'caption-s1', 'material_id': 'caption-m', 'target_timerange': {'start': 1000000, 'duration': 2000000},
         'render_index': 10000, 'clip': {'scale': {'x': 1., 'y': 1.}, 'transform': {'x': 0., 'y': -.7}}},
        {'id': 'caption-s2', 'material_id': 'caption-m', 'target_timerange': {'start': 5000000, 'duration': 2000000},
         'render_index': 10000, 'clip': {'scale': {'x': 1., 'y': 1.}, 'transform': {'x': 0., 'y': -.7}}}]})
    data = {'id': str(uuid.uuid4()), 'version': 360000, 'new_version': '189.0.0',
        'name': 'Generated acceptance fixture', 'duration': DURATION, 'fps': 60.,
        'canvas_config': {'width': 1080, 'height': 1920, 'ratio': '9:16'},
        'render_index_track_mode_on': True, 'tracks': tracks,
        'materials': {'videos': [{'id': 'picture-m', 'type': 'video', 'path': str(directory/'picture.mp4'),
            'duration': DURATION, 'width': 270, 'height': 480, 'has_audio': False}],
            'audios': [audio_mat('voice-m', audio/'voice.wav', 10000000),
                audio_mat('environment-m', directory/'environment.wav', 10000000),
                audio_mat('music-m', directory/'music.wav', DURATION),
                audio_mat('short-m', directory/'short.wav', 400000),
                audio_mat('processed-m', directory/'processing-input.wav', 8000000),
                {'id': 'compound-shell', 'type': 'music', 'path': '', 'duration': 2000000}],
            'drafts': [{'id': 'compound-m', 'type': 'combination', 'draft': child}],
            'vocal_separations': [{'id': 'processing-node', 'type': 'vocal_separation',
                'removed_sounds': ['human'], 'final_algorithm': 'instrument_separate',
                'production_path': str(audio/'processing-output.wav'),
                'time_range': {'start': 2000000, 'duration': 4000000}}],
            'texts': [{'id': 'caption-m', 'type': 'subtitle', 'content': content, 'font_size': 16.}]}}
    roles = {row['key']: '字幕' if row['kind'] == 'text' else '背景音乐' if row['local_key'].startswith('music:') else '保留原声音'
             for row in inspect(data)}
    expected = np.zeros(12*RATE)
    expected[:len(voice)] += voice
    expected[:len(environment)] += environment
    expected[4*RATE:4*RATE+len(short)] += short
    expected[6*RATE:7*RATE] += processing[round(1.5*RATE):round(2.5*RATE)]
    expected[10*RATE:12*RATE] += compound
    return data, roles, {'without_music': expected, 'music': music, 'expected_decoded_sha256': expected_files}


def _correlation(expected, actual):
    import numpy as np
    n = min(len(expected), len(actual))
    if n < RATE//4 or min(np.std(expected[:n]), np.std(actual[:n])) < 1e-8:
        return {'correlation': 0., 'lag_samples': None, 'lag_milliseconds': None}
    x, y = expected[:n], actual[:n]
    size = 1 << (2*n-1).bit_length()
    cross = np.fft.irfft(np.fft.rfft(x, size)*np.conj(np.fft.rfft(y, size)), size)
    # AAC/resampling can shift a decoded waveform by a few samples. Observe and
    # report a bounded lag; never alter the output to force a passing comparison.
    lags = np.arange(-32, 33)
    lag = int(lags[np.argmax(cross[lags % size])])
    x, y = x[max(lag, 0):], y[max(-lag, 0):]
    count = min(len(x), len(y))
    return {'correlation': float(np.corrcoef(x[:count], y[:count])[0, 1]),
            'lag_samples': lag, 'lag_milliseconds': lag/RATE*1000}


def run_test(adapter, app_directory, output_root, cancel=None, log=lambda value: None):
    import numpy as np
    root = validate_output_root(output_root)
    root.mkdir(parents=True, exist_ok=True)
    work = root/('兼容自测_'+time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
    work.mkdir()
    report = {'schema': 1, 'started': now(), 'state': 'starting', 'success': False,
        'generated_assets_only': True, 'user_draft_library_read': False,
        'scope': 'Synthetic compatibility test; real-draft/native and cross-machine acceptance are separate',
        'os': platform.platform(), 'python_bundled': bool(getattr(sys, 'frozen', False)),
        'application_version': __import__('jyexport').__version__, 'directory': str(work)}
    started = time.monotonic()
    def update(state, **fields):
        report.update(state=state, **fields)
        atomic_json(work/'自测报告.json', report)
        log(state)
        if cancel is not None and cancel.is_set():
            raise Cancelled('自测已停止，保留当前报告')
    try:
        update('checking_runtime', profile=adapter.verify())
        for name in ('ffmpeg.exe', 'ffprobe.exe'):
            if not (Path(app_directory)/name).is_file():
                raise ValueError('缺少核验工具 '+name+'；请在界面绑定本机剪映版本目录')
        update('generating_synthetic_assets')
        project = work/'生成素材'
        data, roles, oracle = fixture(project, app_directory)
        # Author the synthetic timeline through the SDK model so its required
        # default control materials/references are present. This is not used to
        # normalize or silently repair a user's draft.
        data = adapter._roundtrip_model(adapter._roundtrip_model(data))
        if {row['key'] for row in inspect(data)} != set(roles):
            raise ValueError('SDK建模改变了合成片段标识')
        atomic_json(work/'合成源文档.json', data)
        authored = copy.deepcopy(data)
        prepared, processing = bind_referenced_results(data, project, work/'处理节点结果')
        prepared, direct = bind_referenced_media_results(prepared, project, work/'处理音频素材')
        for evidence in [*processing['bindings'], *direct['bindings']]:
            info = evidence['wave']
            if info['decoded_sha256'] != oracle['expected_decoded_sha256'][info['source']]:
                raise ValueError('合成处理结果未还原为已知原始WAVE')
        if data != authored:
            raise ValueError('准备过程改变了来源文档')
        media = required_media_snapshot(prepared)
        normalized = adapter._roundtrip_model(prepared)
        normalized_scopes = dict(documents(normalized))
        for scope, document in documents(prepared):
            active = local_references(document)
            differences = meaningful_changes(render_projection(document, active), render_projection(normalized_scopes[scope], active))
            if differences:
                raise ValueError('模型未保留合成测试有效字段：'+str(differences[:5]))
        report.update(processing=processing, direct_results=direct, required_media=media)
        paths = {}
        for key in ('V2', 'V3'):
            paths[key] = work/(key+'.json')
            atomic_json(paths[key], transform(prepared, roles, key))
        srt = work/'字幕.srt'
        srt.write_text(subtitle_srt(prepared, roles), encoding='utf-8-sig', newline='\n')
        update('rendering_two_masters')
        render_started = time.monotonic()
        renders, failures = {}, []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(adapter.render, path, work/(key+'.mp4'), work/('process-'+key), cancel=cancel): key
                       for key, path in paths.items()}
            for future in as_completed(futures):
                try:
                    renders[futures[future]] = future.result()
                except Exception as exc:
                    failures.append(str(exc))
                report['renders'] = renders
                atomic_json(work/'自测报告.json', report)
        report['render_seconds'] = time.monotonic()-render_started
        if failures:
            raise ValueError('; '.join(failures))
        update('checking_audio_safety')
        audio_safety = {key: assert_audio_safe(work/(key+'.mp4'), app_directory, key)
                        for key in ('V2', 'V3')}
        silence_path = work/'数字静音对照.wav'
        _tone(silence_path, .1, 440, 0)
        silence = audio_peak_diagnostics(silence_path, app_directory)
        if not silence.get('safe') or silence.get('digital_silence') is not True:
            raise ValueError('数字静音对照未通过音频安全检查')
        report['audio_safety'] = {**audio_safety, 'digital_silence_control': silence}
        update('composing_and_decoding')
        bundle = compose_bundle(work/'V2.mp4', work/'V3.mp4', srt, work/'输出', app_directory,
            'Generated compatibility test', True, log)
        # Correct provenance: this is generated SDK test output.
        for key in ('V2', 'V3'):
            bundle['variants'][key]['source'] = '程序生成素材的SDK测试母版'
        atomic_json(Path(bundle['directory'])/'两母版交付报告.json', bundle)
        update('checking_known_audio_and_caption_pixels', bundle_report=str(Path(bundle['directory'])/'两母版交付报告.json'))
        def pcm(key):
            raw = _run(app_directory, ['-i', str(work/(key+'.mp4')), '-vn', '-ac', '1', '-ar', str(RATE), '-f', 'f32le', '-'])
            return np.frombuffer(raw, '<f4').astype(float)
        a2, a3 = pcm('V2'), pcm('V3')
        windows = [(1., 2.8), (4.05, 4.35), (6.1, 6.9), (10.1, 11.8)]
        correlations = []
        for left, right in windows:
            lo, hi = round(left*RATE), round(right*RATE)
            correlations.append({'window_seconds': [left, right],
                'retained_audio_correlation': _correlation(oracle['without_music'][lo:hi], a2[lo:hi]),
                'music_difference_correlation': _correlation(oracle['music'][lo:hi], a3[lo:hi]-a2[lo:hi])})
        images = {}
        for key in ('V2', 'V3'):
            raw = _run(app_directory, ['-ss', '2', '-i', str(work/(key+'.mp4')), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
            images[key] = np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3)
        difference = np.abs(images['V2'].astype(np.int16)-images['V3'].astype(np.int16))
        bottom_changed = int(np.count_nonzero(np.max(difference[1200:], axis=2) > 30))
        top_error = float(difference[:1100].mean())
        checks = {'audio_windows': correlations, 'caption_bottom_changed_pixels': bottom_changed,
            'outside_caption_mean_pixel_error': top_error,
            'caption_toggle_observed': bottom_changed > 500 and top_error < 4,
            'decoded_waves_match_authored_files': True}
        if required_media_snapshot(prepared) != media:
            raise ValueError('测试过程中素材发生变化')
        report['checks'] = checks
        if not all(item['retained_audio_correlation']['correlation'] > .99 and item['music_difference_correlation']['correlation'] > .98 for item in correlations):
            raise ValueError('合成音频未达到已知波形对照阈值')
        if not checks['caption_toggle_observed']:
            raise ValueError('未确认字幕像素切换，请检查报告与成片')
        update('passed', success=True, total_seconds=time.monotonic()-started)
    except Exception as exc:
        report.update(state='failed', success=False, error=str(exc), total_seconds=time.monotonic()-started)
        atomic_json(work/'自测报告.json', report)
        log(str(exc))
    summary = ('自动兼容自测通过' if report['success'] else '自动兼容自测未通过')
    summary += '\n\n此结果只证明本机的生成素材测试，不代替真实短剧及剪映原生成片验收。\n'
    summary += '\n报告：自测报告.json\n状态：'+report['state']+'\n'
    if report.get('error'):
        summary += '原因：'+report['error']+'\n'
    (work/'请先看结果.txt').write_text(summary, encoding='utf-8-sig')
    return report


def cli(argv=None):
    import argparse
    import os
    from .discovery import load_json, resolve_app_dir, resource_dirs
    from .engine import component_root
    parser = argparse.ArgumentParser(description='公司兼容自测，不扫描或编辑草稿')
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--runtime-dir')
    parser.add_argument('--app-dir')
    parser.add_argument('--show-result', action='store_true')
    args = parser.parse_args(argv)
    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    from .deployment import configuration_directory
    user=configuration_directory(base)
    settings = load_json(user/'settings.json', {})
    component = component_root(base)
    runtime = load_json(user/'engine_runtime.local.json', {}) or load_json(component/'runtime.local.json', {})
    adapter = EngineAdapter(component/'bin', args.runtime_dir or settings.get('engine_runtime') or runtime.get('runtime_directory', ''), resource_dirs()['cache'])
    report = run_test(adapter, resolve_app_dir(args.app_dir or settings.get('app_dir'), base), args.output_root,
                      log=lambda value: print(value, flush=True))
    if args.show_result:
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk(); window.withdraw()
        text = ('自动自测通过；仍需一集真实草稿及原生对照。' if report['success'] else '自动自测未通过：'+report.get('error', '查看报告'))
        messagebox.showinfo('公司兼容自测', text+'\n\n结果目录：'+report['directory'])
        window.destroy()
    print(json.dumps({'success': report['success'], 'state': report['state'], 'directory': report['directory']}, ensure_ascii=False))
    return 0 if report['success'] else 1
