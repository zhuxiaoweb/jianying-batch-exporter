"""Publish sealed per-episode outputs into a frozen series review layout.

The original complete report stays in the episode's private records. Moves are
idempotent from that report, so interruption never requires another encode.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .archive_layout import is_grouped,validate_layout,resolved_path
from .draft import VARIANTS
from .storage import atomic_json,sha


def publish_grouped(job):
    from .advanced import REVISION,delivery_plan,assert_decoder_cache_safe
    assert_decoder_cache_safe(job.get('headless') or {})
    if not is_grouped(job) or (job.get('headless') or {}).get('grouped_published'):
        return
    from .delivery_contract import omitted_srt
    record=job.get('headless') or {}
    if not record.get('complete') or record.get('state')!='complete':
        return
    private=resolved_path(job['directory']);batch=private.parent.parent
    validate_layout(batch,job)
    if (record.get('revision')!=REVISION or record.get('plan_sha256')!=delivery_plan(job)
            or record.get('source_sha256')!=sha(job['backup_content'])
            or job.get('roles_content_sha256')!=record['source_sha256']):
        raise ValueError('按剧归档的成片证明不属于当前备份和计划，未移动文件')
    report_path=resolved_path(record.get('bundle_report',''))
    expected_report_sha=record.get('bundle_report_sha256')
    if not report_path.is_relative_to(private) or sha(report_path)!=expected_report_sha:
        raise ValueError('按剧归档的原始成片报告改变，未移动文件')
    report=json.loads(report_path.read_text('utf-8'))
    if not report.get('complete') or set(report.get('variants',{}))!=set(VARIANTS):
        raise ValueError('按剧归档缺少完整四版证明')
    entries=[]
    for key in VARIANTS:
        item=report['variants'][key];current=job['variants'][key];probe=item.get('probe',{})
        if (not probe.get('full_decode') or
                (probe.get('width'),probe.get('height'),str(probe.get('fps')),probe.get('container'))!=(1080,1920,'60','MP4')
                or (current.get('file'),current.get('sha256'))!=(item.get('file'),item.get('sha256'))):
            raise ValueError('按剧归档的规格、解码或成片记录不一致')
        entries.append((key,item))
    if job['want_srt']:
        item=report.get('srt') or {};current=job.get('srt') or {}
        if not item.get('file') or (current.get('file'),current.get('sha256'))!=(item.get('file'),item.get('sha256')):
            raise ValueError('按剧归档的字幕记录不一致')
        entries.append(('SRT',item))
    elif report.get('srt') is not None or report.get('omissions',{}).get('srt')!=omitted_srt(job):
        raise ValueError('按剧归档的字幕缺项证明不一致')
    # Validate every source/target before moving any of them. A target already
    # matching this sealed receipt is an interrupted publication we can reuse.
    moves=[]
    for key,item in entries:
        source=resolved_path(item['file']);target=Path(job['archive_layout']['targets'][key])
        if not source.is_relative_to(private) or source==private or not item.get('sha256'):
            raise ValueError('按剧归档的来源路径越界')
        if source.exists() and (not source.is_file() or sha(source)!=item['sha256']):
            raise ValueError('已核验的成片在归类前改变：'+str(source))
        if target.exists():
            if not target.is_file() or sha(target)!=item['sha256']:
                raise FileExistsError('归档目标已有不同内容，已保留原文件；请移走冲突文件后继续本批次：'+str(target))
        elif not source.is_file():
            raise FileNotFoundError('归档来源和目标均缺失：'+str(source))
        moves.append((key,item,source,target))
    for key,item,source,target in moves:
        validate_layout(batch,job)
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            # Windows rename fails if the destination exists. No replacement,
            # copy/re-encode, or permanently shared hard link is introduced.
            if os.name=='nt':os.rename(source,target)
            else:
                os.link(source,target);source.unlink()
        if not target.is_file() or sha(target)!=item['sha256']:
            raise ValueError('归类后成片校验失败，已保留任务：'+str(target))
    if sha(report_path)!=expected_report_sha:
        raise ValueError('原始成片报告在归类时改变，已停止并保留文件')
    published=copy.deepcopy(report)
    for key,item,source,target in moves:
        (published['srt'] if key=='SRT' else published['variants'][key])['file']=str(target)
    published['archive_layout']=copy.deepcopy(job['archive_layout'])
    published['delivery_directory']=job['archive_layout']['project_directory']
    target_report=private/'按剧归档报告.json'
    if target_report.exists():
        if json.loads(target_report.read_text('utf-8'))!=published:
            raise ValueError('已有按剧归档报告内容不同，保留文件并停止')
    else:atomic_json(target_report,published)
    for key,item,source,target in moves:
        (job['srt'] if key=='SRT' else job['variants'][key])['file']=str(target)
    record.update(core_bundle_report=str(report_path),core_bundle_report_sha256=expected_report_sha,
        bundle_report=str(target_report),bundle_report_sha256=sha(target_report),grouped_published=True)
