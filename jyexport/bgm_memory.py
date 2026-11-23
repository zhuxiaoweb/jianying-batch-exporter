"""Conservative BGM suggestions; confirmed choices live only in UI round memory.

Reference hashes here are hints, never substitutes for export input checks.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from pathlib import Path

from .draft import materials
from .storage import Cancelled, sha
from .timeline_graph import documents


def canonical_reference(value, source_folder):
    if not isinstance(value, str) or not value:
        return None
    value = re.sub(r'^##_draftpath_placeholder_[^#]+_##',
                   lambda _: str(source_folder), value, count=1)
    # Authored references use Windows paths, even on the test host.
    value = value.replace('/', '\\')
    if not ntpath.isabs(value):
        return None
    return ntpath.normcase(ntpath.normpath(value))


class ReferenceIndex:
    """One analysis operation; omit unavailable identities instead of guessing."""
    def __init__(self, cancel=None):
        self.cancel = cancel
        self.hashes = {}

    def fingerprint(self, value):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled('已停止分析，任务与备份已保存')
        path = Path(value).resolve(strict=True)
        before = path.stat()
        # Hashing giant video carriers solely for checkbox defaults has no UI
        # benefit worth delaying a batch. Such rows remain manually selectable.
        if not path.is_file() or before.st_size > 512 * 1024 * 1024:
            return None
        stamp = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        key = (str(path).casefold(), stamp)
        if key not in self.hashes:
            digest = sha(path)
            after = path.stat()
            if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None
            self.hashes[key] = digest
        return self.hashes[key]

    def enrich(self, data, rows, references, job):
        scopes = dict(documents(data))
        refs = {(tuple(item['scope']), item['material_id'], item['field']): item
                for item in references}

        def identity(scope, mid, field):
            item = refs.get((scope, mid, field))
            if item is None:
                return None
            authored = item['resolved_path'] if item.get('relocated') else item['original_path']
            path = canonical_reference(authored, job['draft']['folder'])
            digest = self.fingerprint(item['resolved_path']) if path else None
            return {'reference': path, 'sha256': digest} if digest else None

        for row in rows:
            row.pop('bgm_reference_token', None)
            if row['kind'] != 'audio' or row['is_container']:
                continue
            try:
                scope = tuple(row['scope'])
                document = scopes[scope]
                source = identity(scope, row['material_id'], 'path')
                if source is None:
                    continue
                lookup = materials(document)
                segments = {f'{track.get("id", ti)}:{seg.get("id", si)}': seg
                            for ti, track in enumerate(document['tracks'])
                            for si, seg in enumerate(track.get('segments', []))}
                segment = segments[row['local_key']]
                processing = []
                for mid in segment.get('extra_material_refs', []):
                    mat = lookup.get(mid, {})
                    if mat.get('type') != 'vocal_separation' or not (
                            mat.get('choice') or mat.get('removed_sounds') or mat.get('final_algorithm')):
                        continue
                    output = identity(scope, mid, 'production_path')
                    if output is None:
                        raise ValueError('No verified identity for processed output')
                    processing.append({key: mat.get(key) for key in
                                       ('choice', 'removed_sounds', 'final_algorithm', 'time_range')}
                                      | {'output': output})
                payload = {'source': source, 'processing': processing}
                row['bgm_reference_token'] = hashlib.sha256(json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            except (OSError, ValueError, KeyError, TypeError):
                # This optional convenience must never block a valid export.
                continue
        return rows


class RoundBgmMemory:
    def __init__(self):
        self.tokens = set()

    def reset(self):
        self.tokens.clear()

    def suggest(self, rows):
        return {row['key'] for row in rows if row.get('bgm_reference_token') in self.tokens
                and row['kind'] == 'audio' and not row['is_container']}

    def remember(self, rows, selected_keys):
        selected = set(selected_keys)
        self.tokens.update(row['bgm_reference_token'] for row in rows
                           if row['key'] in selected and row.get('bgm_reference_token')
                           and row['kind'] == 'audio' and not row['is_container'])


def confirmation_counts(jobs):
    confirmed = sum(bool(job.get('easy_confirmed')) for job in jobs)
    failed = sum(not job.get('easy_confirmed') and bool(job.get('easy_analysis_error')) for job in jobs)
    pending = len(jobs) - confirmed - failed
    text = f'本批任务：已确认 {confirmed} 集，待确认 {pending} 集（含当前）。'
    if failed:
        text += f'另有 {failed} 集分析未通过，可在任务列表查看原因。'
    return text
