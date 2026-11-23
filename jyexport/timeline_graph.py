"""Read-only traversal of explicitly embedded, referenced child timelines."""
from __future__ import annotations

from .draft import materials


def local_references(document):
    """Resolve this timeline's material namespace without entering child drafts."""
    lookup = materials(document)
    for group in document.get('materials', {}).values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict) and 'id' in item and item != lookup[item['id']]:
                raise ValueError('同一时间线存在参数不同的重复素材ID：' + str(item['id']))

    def collect(value):
        if isinstance(value, dict):
            return set().union(*(collect(child) for key, child in value.items() if key != 'draft'))
        if isinstance(value, list):
            return set().union(*(collect(child) for child in value))
        return {value} if isinstance(value, str) and value in lookup else set()

    active = collect({key: value for key, value in document.items() if key != 'materials'})
    pending = list(active)
    while pending:
        for identifier in collect(lookup[pending.pop()]):
            if identifier not in active:
                active.add(identifier)
                pending.append(identifier)
    return active


def is_compound(material):
    return material.get('type') in ('draft', 'compound', 'combination') or 'draft' in material or bool(material.get('draft_id'))


def documents(root, max_depth=16):
    def visit(document,scope,ancestors):
        if not isinstance(document,dict) or not isinstance(document.get('tracks'),list):
            raise ValueError('复合片段没有可识别的内嵌时间线')
        if len(scope)>max_depth or id(document) in ancestors:
            raise ValueError('复合时间线循环或嵌套过深')
        yield scope,document
        lookup=materials(document)
        for mid in sorted(local_references(document)):
            material=lookup[mid]
            if not is_compound(material):
                continue
            child=material.get('draft')
            if not isinstance(child,dict):
                raise ValueError('复合片段只有外部文件引用，尚未验证其加载方式')
            yield from visit(child,scope+(mid,),ancestors|{id(document)})
    yield from visit(root,(),set())
