"""Strict media preflight for candidate embedded-timeline rendering."""
from pathlib import Path
import wave
import json
import subprocess
import math

from .draft import materials
from .storage import sha
from .storage import Cancelled
from .timeline_graph import documents, is_compound, local_references
from .float_wave import inspect_float_wave
from .preflight import check_all


def assert_readable_wave_paths(data):
    """A render must not silently skip authored WAV media, even without AI nodes."""
    seen = set()
    for _, document in documents(data):
        lookup = materials(document)
        for track in document['tracks']:
            if track.get('type') not in ('audio', 'video'):
                continue
            for segment in track.get('segments', []):
                value = lookup.get(segment.get('material_id'), {}).get('path')
                if not value or Path(value).suffix.casefold() != '.wav' or value in seen:
                    continue
                seen.add(value)
                try:
                    with wave.open(str(value), 'rb') as audio:
                        count = audio.getnframes()
                        if count <= 0:
                            raise ValueError('WAV没有采样')
                        audio.setpos(count-1)
                        if len(audio.readframes(1)) != audio.getnchannels()*audio.getsampwidth():
                            raise ValueError('WAV采样数据不完整')
                except (wave.Error, EOFError) as exc:
                    try:inspect_float_wave(value)
                    except (ValueError,EOFError) as float_error:
                        raise ValueError('所用WAV读取校验失败，已停止以免丢失声音：'+str(value)+'（'+str(float_error)+'）') from exc


def required_media_snapshot(data):
    assert_readable_wave_paths(data)
    evidence = {}
    for scope, document in documents(data):
        lookup = materials(document)
        for track in document['tracks']:
            if track.get('type') not in ('video', 'audio'):
                continue
            for segment in track.get('segments', []):
                mid = segment.get('material_id')
                if mid not in lookup:
                    raise ValueError('片段缺少明确的素材：' + str((scope, mid)))
                material = lookup[mid]
                value = material.get('path')
                if not value:
                    refs = [material, *(lookup.get(ref, {}) for ref in segment.get('extra_material_refs', []))]
                    if any(is_compound(item) for item in refs):
                        continue
                    raise ValueError('所用音视频素材没有文件路径：' + str((scope, mid)))
                path = Path(value).resolve(strict=True)
                if not path.is_file():
                    raise ValueError('所用音视频素材不是文件：' + str(path))
                if path.suffix.casefold() == '.wav':
                    with path.open('rb') as stream:
                        header = stream.read(12)
                    if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
                        raise ValueError('所用WAV未转换为可读媒体，不能冒险输出静音：' + str(path))
                if str(path) not in evidence:
                    evidence[str(path)] = {'sha256': sha(path), 'size': path.stat().st_size}
    return evidence


def probe_media_inputs(data,app_directory,cancel=None):
    """Check the actual referenced stream types before an SDK can skip an input."""
    requirements={};bounds={}
    for _,document in documents(data):
        lookup=materials(document)
        for track in document['tracks']:
            if track.get('type') not in ('audio','video'):continue
            for segment in track.get('segments',[]):
                material=lookup.get(segment.get('material_id'),{})
                value=material.get('path')
                if value:
                    path=str(Path(value).resolve(strict=True));requirements.setdefault(path,set()).add(track['type'])
                    source=segment.get('source_timerange') or {}
                    if source.get('duration',0)>0 and material.get('type') not in ('photo','image'):
                        bounds.setdefault(path,[]).append({'segment_id':segment.get('id'),
                            'start_us':int(source.get('start',0)),'duration_us':int(source['duration'])})
                elif not any(is_compound(item) for item in [material,*(lookup.get(ref,{}) for ref in segment.get('extra_material_refs',[]))]):
                    raise ValueError('所用音视频素材没有可读文件路径：'+str(segment.get('material_id')))
        for mid in local_references(document):
            material=lookup[mid]
            if material.get('type')=='vocal_separation' and (material.get('choice') or material.get('removed_sounds') or material.get('final_algorithm')):
                value=material.get('production_path')
                if not value:raise ValueError('启用的声音处理没有可读结果')
                requirements.setdefault(str(Path(value).resolve(strict=True)),set()).add('audio')
    tool=Path(app_directory)/'ffprobe.exe'
    if not tool.is_file():raise FileNotFoundError('缺少ffprobe.exe，不能检查输入媒体')
    def inspect(item):
        path,kinds=item
        if cancel is not None and cancel.is_set():raise Cancelled('输入素材检查已停止')
        process=subprocess.run([str(tool),'-v','error','-show_entries',
            'stream=codec_type,codec_name,sample_rate,channels,width,height:format=duration',
            '-of','json',path],stdin=subprocess.DEVNULL,capture_output=True,timeout=60,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:raise ValueError('所用素材无法读取，停止以免丢失声画：'+path)
        parsed=json.loads(process.stdout)
        found={item.get('codec_type') for item in parsed.get('streams',[])}
        if not kinds.issubset(found):raise ValueError('所用素材缺少草稿要求的媒体流：'+path+' / '+','.join(sorted(kinds)))
        declared=parsed.get('format',{}).get('duration')
        try:duration=float(declared)
        except (ValueError,TypeError):duration=0
        if math.isfinite(duration) and duration>0:
            for interval in bounds.get(path,[]):
                if interval['start_us']<0 or (interval['start_us']+interval['duration_us'])/1e6>duration+max(.05,duration*.001):
                    raise ValueError('所选素材不能覆盖草稿的源范围，请核对定位文件：'+path)
        return path,{'required_stream_types':sorted(kinds),'streams':parsed['streams'],
                     'duration':declared,'source_ranges':bounds.get(path,[])}
    return dict(check_all(inspect,sorted(requirements.items()),cancel))
