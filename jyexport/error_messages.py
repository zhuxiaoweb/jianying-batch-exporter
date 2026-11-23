"""Actionable operator messages; original failures remain in the task record."""
from __future__ import annotations

import ntpath

from .execution import resource_failure
from .resources import ResourcePressure

MEDIA_SUFFIXES = {'.wav', '.mp3', '.aac', '.m4a', '.flac', '.ogg', '.wma', '.aif', '.aiff',
                  '.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.jpg', '.jpeg', '.png', '.webp'}


def describe_failure(error, job, stage):
    raw = str(error)
    result = {'kind': 'other', 'stage': stage, 'summary': raw, 'message': raw, 'raw_error': raw}
    refs = job.get('easy_analysis', {}).get('media_references', [])
    filename = str(getattr(error, 'filename', '') or '')
    normalized = ntpath.normcase(ntpath.normpath(filename)) if filename else ''
    related = []
    for item in refs:
        paths = [item.get('original_path', ''), item.get('resolved_path', '')]
        if any(path and ((normalized and (ntpath.normcase(ntpath.normpath(path)) == normalized or
                          ntpath.normcase(ntpath.normpath(path)).startswith(normalized.rstrip('\\') + '\\')))
                        or ntpath.basename(path).casefold() in raw.casefold()) for path in paths):
            related.append(item)

    if isinstance(error, (ResourcePressure, MemoryError)) or resource_failure(error):
        result.update(kind='resources', summary='本机内存/显存余量不足，释放资源后可继续',
            message='本机内存或显存余量不足，本集暂未完成。\n\n请先保存工作并正常关闭多余应用，再点击“继续上次任务”。'
                    '已完成的文件和合格母版会保留；继续时会核对并复用。资源保护和并发自动降级仍有效。')
    elif isinstance(error, FileNotFoundError) and (related or
            ntpath.splitext(filename)[1].casefold() in MEDIA_SUFFIXES or '所用缓存缺失' in raw):
        if not related and '所用缓存缺失' in raw:
            related = [item for item in refs if not item.get('exists')]
        name = ntpath.basename(filename) if filename else (related[0].get('name') if related else '剪映缓存')
        result.update(kind='missing_media', summary='素材引用丢失：'+str(name),
            message='未找到本集引用的素材，已停止本集以免丢失声画。\n\n'
                    '请在剪映中打开“'+job['draft']['name']+' / '+job['draft']['timeline_name']+'”，单独检查并恢复或重新链接该素材。\n\n'
                    '恢复原文件到原路径后，可点击“继续上次任务”。如果在剪映中重新链接或编辑了草稿，请保存草稿后“重新选集”创建新任务；旧任务仍使用原备份。')
    elif raw.startswith('所用素材无法读取') or raw.startswith('所用素材缺少草稿要求的媒体流'):
        name = ntpath.basename(filename) if filename else (related[0].get('name') if related else '组合视频素材')
        result.update(kind='unreadable_media', summary='组合视频素材无法读取：'+str(name),
            message='本集引用的组合视频素材无法读取，已停止本集以免丢失画面或声音。\n\n'
                    '请在剪映中打开“'+job['draft']['name']+' / '+job['draft']['timeline_name']+'”，进入对应复合片段检查主视频文件是否能正常播放。\n\n'
                    '优先在剪映副本中解除复合片段并保存，再“重新选集”创建新任务。若保留复合片段，请先恢复原路径中的可播放主视频文件并重新创建批次。不要用同名 alpha 文件替代主视频。')
    elif '音频分段参数未通过文件校验' in raw or '声音缓存格式校验未通过' in raw:
        name = ntpath.basename(related[0]['original_path']) if related else raw.split('：')[0]
        result.update(kind='unverified_audio', summary='声音缓存格式尚未验证：'+str(name),
            message='该加密声音结果的分段参数未通过校验，本集已停止，原文件和任务保留。\n\n'
                    '可先在剪映专业版原生导出“'+job['draft']['name']+' / '+job['draft']['timeline_name']+'”。如需助手继续处理，可在草稿副本中重新生成这段声音处理结果并保存，再“重新选集”创建新任务。\n\n'
                    '如果必须保留当前结果，请同时提供与该 WAV 文件同名、同一时间段的完整“_download.wav”明文核对副本，并保留原加密 WAV。不能使用相邻时间段的 WAV 或其他 human.wav 替代。')
    elif '片段超出实际PCM数据' in raw:
        result.update(kind='unverified_audio', summary='处理声音的PCM范围不足',
            message='该声音处理结果的PCM长度不足以覆盖草稿片段，本集已停止。\n\n'
                    '请在剪映中重新生成该声音处理结果并保存，再重新选集创建任务。工具不会补采样或裁剪片段。')
    elif '引用素材不存在' in raw:
        result.update(kind='missing_media_reference', summary='草稿引用了不存在的素材',
            message='本集或子草稿引用的素材定义不存在，本集已停止。\n\n'
                    '请在剪映中打开对应集，重新链接素材并保存草稿，再重新选集创建任务。')
    elif '处理结果时长短于草稿声明范围' in raw:
        result.update(kind='unverified_audio', summary='处理声音结果长度不足',
            message='本集的声音处理结果没有覆盖草稿声明的完整范围，已停止本集。\n\n'
                    '请在剪映中重新生成该时间段的声音处理结果并保存，再点击“重新选集”创建新任务。')
    elif '同一时间线存在参数不同的重复素材ID' in raw:
        material_id = raw.rsplit('：', 1)[-1].strip()
        result.update(kind='duplicate_material_reference', summary='草稿素材引用需要重新分析：'+material_id,
            message='本集的素材 ID 在同一时间线中出现了重复参数记录，已停止本集。\n\n'
                    '请先点击“重新选集”重新读取当前草稿。若仍然失败，请在剪映中保存该集后重新创建批次。工具不会合并或猜测两个素材定义。')
    elif '色彩匹配' in raw or any('/color_match_info/' in item for item in
                                         getattr(error, 'compatibility_details', {}).get('changes', [])):
        result.update(kind='color_match_compatibility', summary='色彩匹配参考参数尚未兼容，请查看详情',
            message=raw+'\n\n请查看本集“兼容性诊断.json”。可先在剪映原生导出该集；工具会保留本集和其他集的进度。')

    if result['kind'] != 'other':
        result['references'] = related
        details = []
        for item in related[:8]:
            line = '引用：'+str(item.get('original_path') or item.get('resolved_path'))
            if item.get('resolved_path') != item.get('original_path'):
                line += '\n当前读取位置：'+str(item.get('resolved_path'))
            for position in item.get('positions', [])[:3]:
                line += '\n轨道：'+str(position.get('track'))+'；'+('复合内' if item.get('scope') else '时间线')+f' {position["start"]/1e6:.3f} 秒'
            details.append(line)
        if not related and filename:
            details.append('读取路径：'+filename)
        if details:
            result['message'] += '\n\n'+'\n\n'.join(details)
        result['message'] += '\n\n原始信息：'+raw
    return result
