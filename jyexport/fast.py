"""Reuse two native diagonal variants without decoding/re-encoding their streams."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import uuid
from fractions import Fraction
from pathlib import Path

from .draft import VARIANTS
from .media import probe, read_srt
from .storage import atomic_json, now, sha


def validate_output_root(output_root, sources=()):
    """Validate the actual write location before creating any result files."""
    if not str(output_root).strip():
        raise ValueError('请选择独立的交付目录，输出目录不能为空')
    root = Path(output_root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError('输入输出重叠：交付目录指向现有文件')
    if any(root == Path(source).expanduser().resolve() for source in sources if source):
        raise ValueError('输入输出重叠：交付目录不能等于来源文件')
    if any((directory / 'draft_content.json').is_file() for directory in (root, *root.parents)):
        raise ValueError('交付目录不能位于剪映草稿内部')
    return root


def _source_snapshot(path):
    path = Path(path)
    before = path.stat()
    digest = sha(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError('来源文件仍在写入，请等待导出完成后重试')
    return (after.st_size, after.st_mtime_ns, digest)


def _check_sources(paths, snapshots):
    for key, path in paths.items():
        if _source_snapshot(path) != snapshots[key]:
            raise RuntimeError('母版或字幕在处理过程中发生变化，未完成本批次验收')


def _run(app_dir, executable, arguments, timeout=3600):
    command = Path(app_dir) / executable
    if not command.is_file():
        raise FileNotFoundError(f'缺少 {executable}')
    result = subprocess.run([str(command), *arguments], capture_output=True,
        encoding='utf-8', errors='replace', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(result.stderr[-1800:] or f'{executable}执行失败')
    return result.stdout


def stream_evidence(path, app_dir):
    """Hash encoded packet payloads together with exact rational timestamps."""
    text = _run(app_dir, 'ffprobe.exe', ['-v', 'error', '-show_streams',
        '-show_packets', '-show_data_hash', 'sha256', '-show_entries',
        'stream=index,codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,sample_aspect_ratio,avg_frame_rate,r_frame_rate,time_base,start_pts,duration_ts,nb_frames,sample_rate,channels,channel_layout:packet=stream_index,pts,dts,duration,data_hash:packet_side_data=side_data_type,skip_samples,discard_padding',
        '-of', 'json', str(path)])
    data = json.loads(text)
    streams = data.get('streams', [])
    if len(streams) != 2 or sorted(s.get('codec_type') for s in streams) != ['audio', 'video']:
        raise ValueError('两母版模式要求每个文件恰好一条视频流和一条音频流')
    result = {}
    for stream in streams:
        clock = Fraction(stream['time_base'])
        rows, times, payloads = [], [], []
        for packet in data.get('packets', []):
            if packet['stream_index'] != stream['index']:
                continue
            if any(packet.get(key) is None for key in ('pts', 'dts', 'duration', 'data_hash')):
                raise ValueError('媒体包缺少明确时间戳，不能安全复用')
            timing = [str(int(packet[key]) * clock) for key in ('pts', 'dts', 'duration')]
            side = packet.get('side_data_list', [])
            rows.append([*timing, packet['data_hash'], side])
            times.append(timing)
            payloads.append(packet['data_hash'])
        if not rows:
            raise ValueError('媒体流没有有效数据包')
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()
        result[stream['codec_type']] = {'metadata': stream, 'packets': len(rows),
            'packet_and_time_sha256': digest(rows), 'time_sha256': digest(times),
            'payload_sha256': digest(payloads)}
    return result


def check_pair(evidence2, evidence3):
    """Require matching clocks, timelines and video/color/audio configuration."""
    for kind, fields in (
        ('video', ('codec_name', 'width', 'height', 'pix_fmt', 'color_space',
                   'color_transfer', 'color_primaries', 'sample_aspect_ratio',
                   'avg_frame_rate', 'r_frame_rate', 'time_base', 'start_pts', 'duration_ts', 'nb_frames')),
        ('audio', ('codec_name', 'sample_rate', 'channels', 'channel_layout',
                   'time_base', 'start_pts', 'duration_ts', 'nb_frames'))):
        a, b = evidence2[kind], evidence3[kind]
        for field in fields:
            if a['metadata'].get(field) != b['metadata'].get(field):
                raise ValueError(f'两母版的{kind}/{field}不一致，请使用同集同参数重新导出')
        if a['time_sha256'] != b['time_sha256']:
            raise ValueError(f'两母版{kind}数据包时序不一致，停止复用，需四次原生导出')


def compose_pair(v2, v3, destination, app_dir, expected_duration=0,
                 variants=('V1', 'V4'), log=lambda value: None, on_variant=None):
    """Create only missing diagonal variants in an existing/new output folder."""
    started = time.perf_counter()
    v2, v3, destination = Path(v2).resolve(), Path(v3).resolve(), Path(destination).resolve()
    if v2 == v3:
        raise ValueError('V2和V3必须是两份独立的原生导出文件')
    if not variants or any(key not in ('V1', 'V4') for key in variants):
        raise ValueError('只能从两母版补齐V1、V4')
    validate_output_root(destination, (v2, v3))
    targets = {key: destination / f'{key}_{VARIANTS[key]["label"]}.mp4' for key in variants}
    if any(path.exists() for path in targets.values()):
        raise FileExistsError('目标成片已存在，拒绝覆盖；请选择新目录或恢复已完成批次')
    source_stats = {key: _source_snapshot(p)
                    for key, p in (('V2', v2), ('V3', v3))}
    log('核对两母版规格、包时序和媒体指纹')
    for path in (v2, v3):
        probe(path, app_dir, expected_duration, decode=False)
    source_evidence = {'V2': stream_evidence(v2, app_dir), 'V3': stream_evidence(v3, app_dir)}
    check_pair(source_evidence['V2'], source_evidence['V3'])
    source_check_seconds = time.perf_counter() - started
    destination.mkdir(parents=True, exist_ok=True)
    report = {'method': 'two_native_masters_stream_copy', 'created': now(),
        'sources': {key: {'file': str(path), 'sha256': source_stats[key][2],
                         'bytes': source_stats[key][0], 'mtime_ns': source_stats[key][1],
                         'streams': source_evidence[key]}
                    for key, path in (('V2', v2), ('V3', v3))},
        'source_check_seconds': source_check_seconds, 'variants': {},
        'video_reencoded': False, 'audio_reencoded': False, 'reviewed': False}
    for key in variants:
        video_key, audio_key = ('V3', 'V2') if key == 'V1' else ('V2', 'V3')
        sources = {'V2': v2, 'V3': v3}
        partial = destination / ('.' + key + '_' + uuid.uuid4().hex + '.partial.mp4')
        log(f'{key}：复用{video_key}画面与{audio_key}声音，不重新编码')
        mux_start = time.perf_counter()
        _run(app_dir, 'ffmpeg.exe', ['-nostdin', '-v', 'error', '-n',
            '-i', str(sources[video_key]), '-i', str(sources[audio_key]),
            '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
            '-movflags', '+faststart', str(partial)])
        mux_seconds = time.perf_counter() - mux_start
        evidence = stream_evidence(partial, app_dir)
        for kind, source_key in (('video', video_key), ('audio', audio_key)):
            if evidence[kind]['packet_and_time_sha256'] != source_evidence[source_key][kind]['packet_and_time_sha256']:
                raise ValueError(f'{key}的{kind}数据包或时间戳变化，保留临时文件并停止交付')
        log(f'{key}：逐包复用核验通过，执行完整解码')
        checked = probe(partial, app_dir, expected_duration, decode=True)
        # Do not publish/register a variant using a source that changed while
        # it was being read. Earlier completed variants keep their own receipt.
        _check_sources({'V2': v2, 'V3': v3}, source_stats)
        if targets[key].exists():
            raise FileExistsError(targets[key])
        partial.rename(targets[key])
        report['variants'][key] = {'file': str(targets[key]), 'sha256': sha(targets[key]),
            'video_source': video_key, 'audio_source': audio_key, 'probe': checked,
            'mux_seconds': mux_seconds, 'streams': evidence,
            'status': '规格通过·待声画审核', 'reviewed': False}
        if on_variant:
            on_variant(key, report['variants'][key], report['sources'])
    _check_sources({'V2': v2, 'V3': v3}, source_stats)
    report['total_seconds'] = time.perf_counter() - started
    report['sources_unchanged'] = True
    return report


def compose_bundle(v2, v3, srt, output_root, app_dir, identity,
                   confirmed_pair=False, log=lambda value: None, *, srt_omission=None):
    """A portable handoff mode: no draft reader, AI or desktop control required."""
    if not confirmed_pair:
        raise ValueError('请确认两母版来自同集，时间线完全相同，仅切换BGM和字幕')
    if not identity.strip():
        raise ValueError('请填写项目与集号')
    if not srt and not (isinstance(srt_omission,dict) and srt_omission.get('confirmed') is True
                        and srt_omission.get('reason')=='no_exportable_subtitles'):
        raise ValueError('未提供SRT；无字幕草稿须先明确确认缺项，不能生成空字幕替代。')
    if srt and srt_omission:raise ValueError('字幕文件与无字幕确认冲突')
    started = time.perf_counter()
    output_root = validate_output_root(output_root, (v2, v3, srt))
    srt_snapshot = _source_snapshot(srt) if srt else None
    if srt:
        read_srt(srt)
        _check_sources({'SRT': srt}, {'SRT': srt_snapshot})
    from .storage import safe_name
    folder = Path(output_root).resolve() / ('快速交付_' + safe_name(identity) + '_' +
             time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True, exist_ok=False)
    atomic_json(folder / '交付状态.json', {'status': '处理中', 'identity': identity})
    try:
        report = compose_pair(v2, v3, folder, app_dir, log=log)
        report['identity'] = identity
        report['complete'] = False
        for key, source in (('V2', v2), ('V3', v3)):
            source = Path(source).resolve()
            target = folder / f'{key}_{VARIANTS[key]["label"]}.mp4'
            shutil.copy2(source, target)
            if sha(target) != report['sources'][key]['sha256']:
                raise RuntimeError('母版归档复制校验失败')
            checked = probe(target, app_dir, decode=True)
            report['variants'][key] = {'file': str(target), 'sha256': sha(target),
                'probe': checked, 'status': '规格通过·待声画审核', 'reviewed': False,
                'source': '剪映原生母版，文件直接复制'}
        if srt:
            duration_us = round(report['variants']['V2']['probe']['duration'] * 1_000_000)
            text, cues = read_srt(srt, duration_us)
            _check_sources({'SRT': srt}, {'SRT': srt_snapshot})
            target = folder / '字幕.srt'
            target.write_text(text.replace('\r\n', '\n'), encoding='utf-8-sig', newline='\n')
            report['srt'] = {'file': str(target), 'sha256': sha(target), 'cues': cues, 'status': '待审核'}
        else:
            report['srt']=None
            report['omissions']={'srt':dict(srt_omission)}
            (folder/'缺项说明.txt').write_text(srt_omission['status']+'\n仍保留四版文件名供团队归档；没有新增、识别或伪造字幕。\n',encoding='utf-8-sig')
        report['status'] = '五项文件完成·待声画审核' if srt else '四版视频完成·无字幕未生成SRT·待声画审核'
        report['directory'] = str(folder)
        report['bundle_total_seconds'] = time.perf_counter() - started
        import csv
        with (folder / '审核归档总表.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['项目/集', '版本', '文件', 'SHA256', '状态'])
            for key in ('V1', 'V2', 'V3', 'V4'):
                item = report['variants'][key]
                writer.writerow([identity, key, item['file'], item['sha256'], item['status']])
            writer.writerow([identity, 'SRT', report['srt']['file'] if srt else '',
                             report['srt']['sha256'] if srt else '', '待审核' if srt else srt_omission['status']])
        snapshots = {key: (item['bytes'], item['mtime_ns'], item['sha256'])
                     for key, item in report['sources'].items()}
        _check_sources({'V2': v2, 'V3': v3}, snapshots)
        if srt:_check_sources({'SRT': srt}, {'SRT': srt_snapshot})
        report['complete'] = True
        report['bundle_total_seconds'] = time.perf_counter() - started
        atomic_json(folder / '两母版交付报告.json', report)
        atomic_json(folder / '交付状态.json', {'status': report['status'], 'identity': identity, 'complete': True})
        return report
    except Exception as exc:
        failure = {'status': '失败·未完成交付', 'identity': identity, 'error': str(exc), 'complete': False}
        if (folder / '两母版交付报告.json').exists():
            report.update(failure)
            atomic_json(folder / '两母版交付报告.json', report)
        atomic_json(folder / '交付状态.json', failure)
        raise


def compose_csv(csv_path, output_root, app_dir, confirmed_pair=False, log=lambda value: None):
    import csv
    if not confirmed_pair:
        raise ValueError('请先确认清单内每组母版来自同集且仅切换配乐与字幕')
    output_root = validate_output_root(output_root)
    csv_path = Path(csv_path).resolve()
    with csv_path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not {'剧名', '集号', 'V2', 'V3', 'SRT'}.issubset(reader.fieldnames or []):
            raise ValueError('CSV必须包含剧名、集号、V2、V3、SRT五列')
        rows = list(reader)
    if not rows or len(rows) > 1000:
        raise ValueError('清单要求1至1000集')
    results = []
    completed_directories = []
    for number, row in enumerate(rows, 1):
        identity = f'第{number}行'
        try:
            if any(not isinstance(row.get(key), str) or not row[key].strip()
                   for key in ('剧名', '集号', 'V2', 'V3', 'SRT')):
                raise ValueError('清单该行有空字段')
            identity = row['剧名'].strip() + '__' + row['集号'].strip()
            log(f'[{number}/{len(rows)}] {identity}')
            paths = []
            for key in ('V2', 'V3', 'SRT'):
                path = Path(row[key].strip())
                paths.append((path if path.is_absolute() else csv_path.parent / path).resolve())
            validate_output_root(output_root, paths)
            if any(source == directory or directory in source.parents
                   for source in paths for directory in completed_directories):
                raise ValueError('本批次已生成的输出不能再次作为同一批输入')
            report = compose_bundle(*paths, output_root, app_dir, identity, True, log)
            results.append({'row': number, 'identity': identity, 'ok': True, 'directory': report['directory']})
            completed_directories.append(Path(report['directory']).resolve())
        except Exception as exc:
            results.append({'row': number, 'identity': identity, 'ok': False, 'error': str(exc)})
            log(f'该集停止，继续其他集：{exc}')
    result = {'created': now(), 'jobs': results, 'completed': sum(item['ok'] for item in results),
              'failed': sum(not item['ok'] for item in results), 'reviewed': False}
    path = Path(output_root).resolve() / ('批量交付结果_' + uuid.uuid4().hex[:10] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, result)
    result['report'] = str(path)
    return result


def cli(argv=None):
    import argparse
    from .discovery import resolve_app_dir
    parser = argparse.ArgumentParser(description='两次原生导出，复用媒体流形成四版；不操作草稿')
    parser.add_argument('--app-dir')
    parser.add_argument('--csv')
    parser.add_argument('--v2')
    parser.add_argument('--v3')
    parser.add_argument('--srt')
    parser.add_argument('--identity')
    parser.add_argument('--out', required=True)
    parser.add_argument('--same-timeline-confirmed', action='store_true')
    args = parser.parse_args(argv)
    app_dir = resolve_app_dir(args.app_dir)
    if not app_dir:
        parser.error('未发现剪映FFmpeg目录，请指定--app-dir')
    log = lambda value: print(value, flush=True)
    if args.csv:
        result = compose_csv(args.csv, args.out, app_dir, args.same_timeline_confirmed, log)
    else:
        if not all((args.v2, args.v3, args.srt, args.identity)):
            parser.error('单集模式需要--v2 --v3 --srt --identity')
        result = compose_bundle(args.v2, args.v3, args.srt, args.out, app_dir,
                                args.identity, args.same_timeline_confirmed, log)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
