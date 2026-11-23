"""Candidate native-copy preparation; not enabled in the 1.4 preview GUI.

The installed editor is the renderer. A copy must first be created by its own
Copy command and verified against the before/after local index. This module
does not register projects, capture the screen, or inject input.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import uuid
from pathlib import Path

from .draft import assert_classified, assert_simple, transform
from .storage import atomic_json, now, sha


def verify_new_native_copy(before_index, current_index, source_id, copy_id):
    old={item['draft_id']:item for item in before_index.get('all_draft_store',[]) if not item.get('tm_draft_removed')}
    current=[item for item in current_index.get('all_draft_store',[])
             if item.get('draft_id')==copy_id and not item.get('tm_draft_removed')]
    if source_id not in old or source_id==copy_id or copy_id in old or len(current)!=1:
        raise ValueError('没有唯一的新原生副本身份，拒绝准备')
    source=Path(old[source_id]['draft_fold_path']).resolve()
    target=Path(current[0]['draft_fold_path']).resolve()
    if target==source or target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('原生副本与来源目录重叠')
    return {'copy_id':copy_id,'source_id':source_id,'folder':str(target),'name':current[0]['draft_name']}


def resolve_copy(index, binding):
    matches=[item for item in index.get('all_draft_store',[])
             if item.get('draft_id')==binding['copy_id'] and not item.get('tm_draft_removed')]
    if len(matches)!=1 or matches[0].get('draft_name')!=binding['name']:
        raise ValueError('副本身份或名称改变，请重新核对')
    # The editor can defer a folder rename until the first open/save.
    return Path(matches[0]['draft_fold_path']).resolve()


def variant_for_native(data, roles, variant):
    if variant not in ('V2','V3'):
        raise ValueError('两母版原生准备仅接受V2或V3')
    assert_simple(data)
    assert_classified(data,roles)
    before=copy.deepcopy(data)
    result=transform(data,roles,variant)
    if result.get('id')!=data.get('id') or result.get('new_version')!=data.get('new_version'):
        raise ValueError('原生准备不得改变时间线身份或结构版本')
    if data!=before:raise RuntimeError('源文档被意外修改')
    return result


def read_native_document(adapter, path):
    """Read final JSON without claiming the independent renderer supports it."""
    path=Path(path).resolve(strict=True)
    fingerprint=sha(path)
    raw=path.read_bytes()
    try:data=json.loads(raw)
    except (ValueError,UnicodeError):
        with tempfile.TemporaryDirectory(prefix='jy_native_read_') as temporary:
            base=Path(temporary);source=base/'input.json';output=base/'output.json'
            source.write_bytes(raw)
            adapter._invoke(['-dc',source,'-o',output],base/'process',60)
            data=json.loads(output.read_bytes())
    if sha(path)!=fingerprint:
        raise ValueError('草稿在读取过程中改变')
    if not isinstance(data,dict) or not isinstance(data.get('tracks'),list) or not isinstance(data.get('materials'),dict) or not data.get('id'):
        raise ValueError('不是可识别的最终时间线文档')
    return data,fingerprint


def commit_prepared_payload(folder, timeline_id, payload, backup_record, journal_path, assert_closed):
    """Write to a fully backed native copy; journal before every atomic replace."""
    folder=Path(folder).resolve(strict=True)
    if not isinstance(payload,bytes) or not payload:raise ValueError('准备内容不能为空')
    timeline=(folder/'Timelines'/timeline_id).resolve()
    if not timeline.is_relative_to(folder) or timeline==folder:
        raise ValueError('时间线路径越界')
    backup=Path(backup_record['backup']).resolve(strict=True)
    records={item['path']:item for item in backup_record['files']}
    targets=[timeline/name for name in ('draft_content.json','draft_content.json.bak','template-2.tmp') if (timeline/name).is_file()]
    if not targets or targets[0].name!='draft_content.json':raise ValueError('缺少主内容文件')
    originals={}
    for path in targets:
        if path.is_symlink():raise ValueError('副本内容文件包含链接，未修改')
        relative=path.relative_to(folder).as_posix()
        record=records.get(relative)
        original=backup/relative
        if not record or sha(original)!=record['sha256'] or sha(path)!=record['sha256']:
            raise ValueError('原生副本或其备份已改变，停止写入')
        originals[str(path)]=original.read_bytes()
    if list(folder.rglob('.locked')) or not assert_closed():
        raise ValueError('没有证明副本已关闭，未修改内容')
    import hashlib
    digest=hashlib.sha256(payload).hexdigest()
    journal={'state':'will_write','folder':str(folder),'timeline_id':timeline_id,'at':now(),
             'before':{path:hashlib.sha256(raw).hexdigest() for path,raw in originals.items()},
             'after':digest,'written':[],'rollback':[]}
    journal_path=Path(journal_path)
    if journal_path.resolve().is_relative_to(folder) or journal_path.resolve().is_relative_to(backup):
        raise ValueError('事务记录必须位于副本和备份之外')
    if journal_path.exists():raise FileExistsError('事务记录已存在，请先核对恢复状态')
    atomic_json(journal_path,journal)
    try:
        for path in targets:
            if sha(path)!=journal['before'][str(path)]:raise ValueError('写入前副本改变')
        for path in targets:
            temporary=path.with_name(path.name+'.prepared-'+uuid.uuid4().hex)
            temporary.write_bytes(payload)
            journal['pending_file']=str(path);atomic_json(journal_path,journal)
            os.replace(temporary,path)
            journal['written'].append(str(path));journal.pop('pending_file',None);atomic_json(journal_path,journal)
        if any(sha(path)!=digest for path in targets):raise ValueError('写入后内容校验失败')
        journal['state']='prepared';atomic_json(journal_path,journal)
        return journal
    except Exception as error:
        journal.update(state='needs_recovery',error=str(error))
        try:atomic_json(journal_path,journal)
        except OSError:pass  # A failed journal flush must not prevent a safe rollback.
        try:closed=assert_closed()
        except Exception:closed=False
        if closed:
            for path in reversed(targets):
                name=str(path)
                if path.is_file() and sha(path)==digest:
                    temporary=path.with_name(path.name+'.restore-'+uuid.uuid4().hex)
                    temporary.write_bytes(originals[name]);os.replace(temporary,path)
                    journal['rollback'].append(name);atomic_json(journal_path,journal)
            if all(path.is_file() and sha(path)==journal['before'][str(path)] for path in targets):
                journal['state']='failed_rolled_back';atomic_json(journal_path,journal)
        raise
