from __future__ import annotations
import json
import re
import shutil
import subprocess
import uuid
import os
from contextlib import contextmanager
from contextvars import ContextVar
from fractions import Fraction
from pathlib import Path
from .storage import sha, event, save_session, validate_session

_active_renders = ContextVar('jy_active_renders', default=None)


@contextmanager
def decode_scheduling(active_renders):
    """Scope CPU sharing to this validation worker, without changing the check."""
    token = _active_renders.set(active_renders)
    try:
        yield
    finally:
        _active_renders.reset(token)


def _decode_threads():
    active = _active_renders.get()
    if active is not None and active():
        return min(4, max(1, (os.cpu_count() or 4) // 3))
    return 0

def probe(path, app_dir, expected_duration=0, decode=False):
    path = Path(path).resolve()
    app_dir = Path(app_dir)
    ffprobe = app_dir / 'ffprobe.exe'
    if not ffprobe.is_file():
        raise FileNotFoundError('所选剪映版本目录中没有 ffprobe.exe，无法核验')
    if path.suffix.lower() != '.mp4':
        raise ValueError('只接受 MP4 文件')
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError('视频文件不存在或为空')
    result = subprocess.run([str(ffprobe),'-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,encoding='utf-8',errors='replace',timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise ValueError('无法读取视频：' + result.stderr[-1200:])
    meta = json.loads(result.stdout)
    videos = [x for x in meta.get('streams',[]) if x.get('codec_type') == 'video' and not x.get('disposition',{}).get('attached_pic')]
    audios = [x for x in meta.get('streams',[]) if x.get('codec_type') == 'audio']
    if len(videos) != 1:
        raise ValueError('要求一条视频流')
    video = videos[0]
    fps = Fraction(video.get('avg_frame_rate','0/1'))
    rotation = int(float(video.get('tags',{}).get('rotate',0)))
    for side in video.get('side_data_list',[]):
        rotation = int(side.get('rotation',rotation))
    if (video.get('width'),video.get('height')) != (1080,1920) or rotation % 360:
        raise ValueError(f'画面规格不合格：{video.get("width")}×{video.get("height")}，旋转 {rotation}°；要求物理像素1080×1920')
    sar = video.get('sample_aspect_ratio','1:1')
    if sar not in ('1:1','N/A','0:1'):
        raise ValueError('视频不是方形像素，无法确认9:16')
    if abs(float(fps) - 60) > 0.001:
        raise ValueError(f'帧率为 {float(fps):.6f}；要求60（59.94不作为60通过）')
    if 'mp4' not in meta.get('format',{}).get('format_name','').split(','):
        raise ValueError('文件容器不是MP4')
    if not audios:
        raise ValueError('视频没有音频流，请核对人声、环境音和音效')
    duration = float(meta.get('format',{}).get('duration',0))
    if duration <= 0:
        raise ValueError('视频时长无效')
    if expected_duration and abs(duration - expected_duration / 1_000_000) > max(0.15, expected_duration / 1_000_000 * .001):
        raise ValueError(f'成片时长 {duration:.3f}s 与草稿 {expected_duration/1_000_000:.3f}s 不符')
    if decode:
        ffmpeg = app_dir / 'ffmpeg.exe'
        if not ffmpeg.is_file():
            raise FileNotFoundError('缺少ffmpeg.exe，不能进行完整解码检查')
        threads = _decode_threads()
        options = ['-threads',str(threads),'-filter_threads','1','-filter_complex_threads','1'] if threads else []
        decoded = subprocess.run([str(ffmpeg),'-nostdin','-v','error','-xerror',*options,'-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],capture_output=True,encoding='utf-8',errors='replace',timeout=3600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if decoded.returncode:
            raise ValueError('完整解码检查失败：' + decoded.stderr[-1200:])
    return {'width':1080,'height':1920,'fps':str(fps),'container':'MP4','duration':duration,'audio_streams':len(audios),'full_decode':decode,**({'decode_threads':threads} if decode else {})}

def archive_video(folder, session, job, variant, source, app_dir, reviewed=False):
    if job.get('archive_layout'):raise ValueError('按剧汇总批次请在主界面继续制作交付包；手动归档入口仅适用原结构。')
    validate_session(folder, session)
    item = job['variants'][variant]
    if item.get('file'):
        raise FileExistsError('该版本已经归档，拒绝覆盖。如需新版本，请创建新的批次。')
    source = Path(source).resolve()
    initial = source.stat()
    evidence = probe(source,app_dir,job['draft'].get('duration',0),decode=True)
    initial_hash = sha(source)
    destination = Path(job['directory']) / (variant + '_' + item['label'] + '.mp4')
    if source == destination:
        if source.stat().st_mtime_ns != initial.st_mtime_ns:
            raise RuntimeError('视频仍在写入，请等待剪映完成导出')
    else:
        if destination.exists():
            raise FileExistsError('目标视频已存在，拒绝覆盖')
        temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
        shutil.copy2(source,temporary)
        if sha(temporary) != initial_hash or sha(source) != initial_hash or source.stat().st_mtime_ns != initial.st_mtime_ns:
            raise RuntimeError('复制时视频发生变化，尚未归档')
        temporary.rename(destination)
    item.update({'file':str(destination),'sha256':initial_hash,'probe':evidence,'reviewed':reviewed,'status':'已审核' if reviewed else '规格通过·待声画审核'})
    event(session,'核验归档视频',job=job['id'],variant=variant,evidence=evidence,content_reviewed=reviewed)
    save_session(folder,session)
    return evidence

def read_srt(source, duration=0):
    source = Path(source).resolve()
    text = None
    for encoding in ('utf-8-sig','utf-16','gb18030'):
        try:
            text = source.read_text(encoding=encoding)
            break
        except UnicodeError:
            pass
    if not text or not re.search(r'^\d{2,}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2,}:\d{2}:\d{2},\d{3}',text,re.M):
        raise ValueError('不是有效的SRT字幕文件')
    cues = 0
    for match in re.finditer(r'(\d{2,}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2,}):(\d{2}):(\d{2}),(\d{3})',text):
        cues += 1
        a = list(map(int,match.groups()))
        start = ((a[0]*60+a[1])*60+a[2])*1000+a[3]
        end = ((a[4]*60+a[5])*60+a[6])*1000+a[7]
        if a[1] >= 60 or a[2] >= 60 or a[5] >= 60 or a[6] >= 60 or end <= start or (duration and end*1000 > duration+150000):
            raise ValueError('SRT包含无效/超出草稿时长的时间码')
    return text, cues


def archive_srt(folder,session,job,source):
    if job.get('archive_layout'):raise ValueError('按剧汇总批次请在主界面继续制作交付包；手动归档入口仅适用原结构。')
    validate_session(folder, session)
    if not job['want_srt']:
        raise ValueError('该任务未选择SRT')
    source = Path(source).resolve()
    text, _ = read_srt(source, job['draft'].get('duration',0))
    destination = Path(job['directory'])/'字幕.srt'
    if destination.exists() and destination.resolve() != source:
        raise FileExistsError('任务字幕已存在，拒绝覆盖')
    if destination.resolve() != source:
        destination.write_text(text.replace('\r\n','\n'),'utf-8-sig',newline='\n')
    job['srt']={'file':str(destination),'sha256':sha(destination),'status':'待审核','source':'剪映原生导出/人工提供'}
    event(session,'归档SRT',job=job['id'])
    save_session(folder,session)
