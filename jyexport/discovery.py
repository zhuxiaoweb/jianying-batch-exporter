from __future__ import annotations
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

@dataclass
class Draft:
    key: str
    name: str
    folder: str
    content: str
    timeline_id: str
    timeline_name: str
    encrypted: bool
    duration: int = 0
    modified: int = 0
    source: str = 'local'

    def dict(self):
        return asdict(self)

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text('utf-8-sig'))
    except (OSError, ValueError):
        return default

def user_data():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'JianyingPro/User Data'

def _path_value(value):
    """Parse a registry path without executing a registry command."""
    text = os.path.expandvars(str(value or '')).strip()
    if text.startswith('"'):
        text = text.split('"', 2)[1]
    else:
        match = re.search(r'\.exe(?:,[-\d]+)?(?:\s.*)?$', text, re.I)
        if match:
            text = text[:match.start()] + '.exe'
    path = Path(text)
    return path.parent if path.suffix.casefold() in ('.exe', '.dll') else path

def registry_install_roots():
    result = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                for keyname in (r'Software\ByteDance\JianyingPro', r'Software\JianyingPro', r'Software\Microsoft\Windows\CurrentVersion\Uninstall\JianyingPro'):
                    try:
                        with winreg.OpenKey(hive, keyname, 0, winreg.KEY_READ | view) as key:
                            for field in ('installDir', 'InstallLocation', 'DisplayIcon', 'UninstallString'):
                                try:
                                    value = winreg.QueryValueEx(key, field)[0]
                                    if value:
                                        result.append(_path_value(value))
                                except OSError:
                                    pass
                    except OSError:
                        pass
    except ImportError:
        pass
    return result

def app_dirs(hint=None):
    roots = [Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro/Apps']
    roots.extend(registry_install_roots())
    for variable in ('JY_INSTALL_DIR', 'JIANYING_INSTALL_DIR', 'JIANYING_INSTALL_ROOT'):
        if os.environ.get(variable):
            roots.append(_path_value(os.environ[variable]))
    if hint:
        p = _path_value(hint).resolve()
        for candidate in [p, *list(p.parents)[:4]]:
            if candidate == Path(candidate.anchor):
                continue
            roots.extend([candidate, candidate / 'JianyingPro', candidate / 'JianyingPro/Apps'])
    found = set()
    for p in roots:
        if not p.is_dir():
            continue
        if (p / 'videoeditor.dll').is_file():
            found.add(str(p.resolve()))
        try:
            for child in p.iterdir():
                if child.is_dir() and re.match(r'^\d+\.\d+', child.name) and (child / 'videoeditor.dll').is_file():
                    found.add(str(child.resolve()))
        except OSError:
            continue
    def version(path):
        return tuple(int(x) for x in re.findall(r'\d+', Path(path).name))
    return sorted(found, key=version, reverse=True)

def resolve_app_dir(selected=None, hint=None):
    if selected:
        path = _path_value(selected).expanduser().resolve()
        if (path / 'videoeditor.dll').is_file():
            return str(path)
        nested = [p for p in app_dirs(path) if path in Path(p).parents]
        if nested:
            return nested[0]
    candidates = app_dirs(hint)
    return candidates[0] if candidates else ''

def resource_dirs():
    return {'cache': str(user_data() / 'Cache'), 'presets': str(user_data() / 'Presets')}

def roots_and_index(hint=None):
    roots, index = set(), {}
    config = user_data() / 'Config/globalSetting'
    try:
        text = config.read_text('utf-8-sig')
        match = re.search(r'^currentCustomDraftPath=(.+)$', text, re.M)
        if match:
            roots.add(match[1].strip().replace('\\\\', '\\'))
    except OSError:
        pass
    default_root = user_data() / 'Projects/com.lveditor.draft'
    roots.add(str(default_root))
    metadata = load_json(default_root / 'root_meta_info.json', {})
    for item in metadata.get('all_draft_store', []):
        if item.get('tm_draft_removed') or item.get('draft_is_invisible') or item.get('draft_is_cloud_temp_draft'):
            continue
        folder = item.get('draft_fold_path')
        if folder:
            index[str(Path(folder).resolve()).casefold()] = item
            declared = item.get('draft_root_path')
            if declared and Path(declared).is_dir():
                roots.add(str(Path(declared)))
            elif (Path(folder) / 'draft_content.json').is_file():
                roots.add(str(Path(folder)))
    if hint:
        p = Path(hint).resolve()
        if (p / 'JianyingPro Drafts').is_dir():
            roots.add(str(p / 'JianyingPro Drafts'))
        elif (p / 'draft_content.json').is_file() or p.name in ('JianyingPro Drafts', 'com.lveditor.draft'):
            roots.add(str(p))
    return sorted({str(Path(x).resolve()) for x in roots if Path(x).is_dir()}), index

def scan(roots, index=None):
    index = index or {}
    folders, result, warnings = set(), [], []
    for root in roots:
        p = Path(root)
        if (p / 'draft_content.json').is_file():
            folders.add(p.resolve())
        else:
            try:
                folders.update(x.resolve() for x in p.iterdir() if x.is_dir() and not x.name.startswith('.') and not x.is_symlink() and (x / 'draft_content.json').is_file())
            except OSError as exc:
                warnings.append(f'{root}: {exc}')
    for folder in sorted(folders):
        item = index.get(str(folder).casefold(), {})
        meta = load_json(folder / 'draft_meta_info.json', {})
        name = item.get('draft_name') or meta.get('draft_name') or folder.name
        project = load_json(folder / 'Timelines/project.json', {})
        timelines = [x for x in project.get('timelines', []) if not x.get('is_marked_delete')]
        if not timelines:
            layout = load_json(folder / 'timeline_layout.json', {})
            for dock in layout.get('dockItems', []):
                timelines.extend({'id': tid, 'name': title} for tid, title in zip(dock.get('timelineIds', []), dock.get('timelineNames', [])))
        if not timelines:
            timelines = [{'id': '', 'name': '主时间线'}]
        seen = set()
        for timeline in timelines:
            tid = timeline.get('id', '')
            if tid in seen:
                continue
            seen.add(tid)
            content = folder / 'Timelines' / tid / 'draft_content.json' if tid else folder / 'draft_content.json'
            if not content.is_file():
                warnings.append(f'{name} / {timeline.get("name", tid)} 缺少时间线文件，已跳过（不使用其他集代替）')
                continue
            try:
                with content.open('rb') as stream:
                    encrypted = not stream.read(128).lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'{')
                data = {} if encrypted else load_json(content, {})
                result.append(Draft(f'{folder}|{tid}', name, str(folder), str(content), tid, timeline.get('name') or tid, encrypted,
                    int(data.get('duration') or (item.get('tm_duration', 0) if len(timelines) == 1 else 0)), content.stat().st_mtime_ns))
            except OSError as exc:
                warnings.append(f'{content}: {exc}')
    return result, warnings
