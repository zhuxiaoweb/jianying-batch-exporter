"""Explicit exceptions for absent authored tracks; never invent missing content."""

NO_SUBTITLES = '未识别出可导出字幕，经确认不生成SRT；相应有/无字幕版本画面相同。'
NO_AUDIO_TRACKS = '未识别出独立音频片段，按草稿现状导出并保留视频自带原声。'


def missing_features(analysis, caption_keys):
    selected = set(caption_keys)
    usable = analysis.get('exportable_caption_keys')
    no_subtitles = not (selected if usable is None else selected.intersection(usable))
    no_audio = not any(row['kind'] == 'audio' for row in analysis['rows'])
    return {'no_subtitles': no_subtitles, 'no_audio_tracks': no_audio}


def omitted_srt(job):
    if job['want_srt']:
        return None
    options = job.get('delivery_options') or {}
    if options.get('no_subtitles_confirmed') is not True or options.get('source_sha256') != job.get('roles_content_sha256'):
        raise ValueError('未识别出字幕，尚未确认是否继续；请返回本集确认提示。')
    return {'reason': 'no_exportable_subtitles', 'confirmed': True, 'status': NO_SUBTITLES}


def delivery_items(job):
    return [*job['variants'].values(), *([job.get('srt') or {}] if job['want_srt'] else [])]


def completion_state(job, reviewed=False):
    text = '完成，已审核' if reviewed else '完成，待声画审核'
    if not job['want_srt']:
        text += '（无字幕，未生成SRT）'
    return text
