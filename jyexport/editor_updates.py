"""Narrow, backed-up edits of Jianying's two existing Qt settings.

This does not control the editor process or updater. A successful result verifies
the on-disk policy at return time, not an editor instance's cached UI state.
QLockFile's first three UTF-8 lines are PID, process name, and hostname; on
Windows Qt strips the executable's extension from the process name.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import uuid


PREFERENCE = 'disable_jianying_auto_update'
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PREFERENCES_BYTES = 2 * 1024 * 1024
LOCK_ATTEMPTS = 4
PREFERENCE_LOCK_ATTEMPTS = 40
LOCK_WAIT_SECONDS = .05


def disables_auto_update(preferences):
    """Only an explicitly saved JSON false permits automatic updates."""
    return not isinstance(preferences, dict) or preferences.get(PREFERENCE) is not False


def _absolute(path):
    # Do not resolve() away a junction before the link checks below.
    return Path(os.path.abspath(os.fspath(path)))


def _no_links(path):
    path = _absolute(path)
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('设置路径包含链接或目录联接，已停止：' + str(candidate))
    return path


def _read_bytes(path, maximum, *, optional=False):
    path = _no_links(path)
    try:
        before = path.stat()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError('尚未找到剪映已有配置，未创建或猜测配置：' + str(path))
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ValueError('设置文件类型异常或体积过大：' + str(path))
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    _no_links(path)
    after = path.stat()
    if len(data) > maximum:
        raise ValueError('设置文件体积过大：' + str(path))
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after) or len(data) != after.st_size:
        raise ValueError('读取时设置已被其他程序更新，请稍后重试：' + str(path))
    return data


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _process_name():
    executable = sys.executable
    if os.name == 'nt':
        # Match Qt's processNameByPid, including a frozen GUI process.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
            raise OSError('无法确定当前进程名称，未创建设置锁')
        executable = buffer.value
    name = Path(executable).name
    return name.rsplit('.', 1)[0] if os.name == 'nt' and '.' in name else name


@contextmanager
def _advisory_lock(path, *, attempts=LOCK_ATTEMPTS):
    """Never remove another writer's lock, even if it appears stale."""
    path = _no_links(path)
    payload = (str(os.getpid()) + '\n' + _process_name() + '\n' +
               os.environ.get('COMPUTERNAME', socket.gethostname()) + '\n').encode('utf-8')
    if b'\r' in payload or len(payload.splitlines()) != 3:
        raise ValueError('无法生成合法的设置锁标识')
    descriptor = None
    identity = None
    payload_ready = False
    try:
        for attempt in range(attempts):
            _no_links(path)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     getattr(os, 'O_BINARY', 0), 0o600)
                identity = os.fstat(descriptor)
                with os.fdopen(os.dup(descriptor), 'wb') as stream:
                    if stream.write(payload) != len(payload):
                        raise OSError('设置锁未完整写入，未修改配置')
                    stream.flush()
                    os.fsync(stream.fileno())
                payload_ready = True
                break
            except FileExistsError:
                if attempt + 1 == attempts:
                    raise OSError('设置正在被其他程序写入，已保留其锁，请稍后重试：' + str(path))
                time.sleep(LOCK_WAIT_SECONDS)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
            # Ownership is checked after closing the Windows handle. A replaced
            # or modified lock belongs to someone else and is deliberately kept.
            try:
                current = path.lstat()
                if (not stat.S_ISLNK(current.st_mode) and
                        not getattr(current, 'st_file_attributes', 0) & 0x400 and
                        (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)):
                    with path.open('rb') as stream:
                        content = stream.read(len(payload) + 1)
                    # A failed write/flush may have left only our payload prefix.
                    # Remove that incomplete owned lock, never a changed payload
                    # or a replacement inode created by another writer.
                    if content == payload or (not payload_ready and payload.startswith(content)):
                        path.unlink()
            except (FileNotFoundError, PermissionError):
                pass


def _parse_ini(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('剪映设置不是已验证的UTF-8格式，未写入') from exc
    if '\x00' in text:
        raise ValueError('剪映设置包含异常字符，未写入')
    lines = text.splitlines(keepends=True)
    sections, keys, current = {}, {}, None
    for number, line in enumerate(lines):
        body = line.rstrip('\r\n')
        stripped = body.strip()
        if not stripped or stripped.startswith((';', '#')):
            continue
        section = re.fullmatch(r'[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#].*)?', body)
        if section:
            current = section.group(1).casefold()
            if current in sections:
                raise ValueError('剪映设置含重复配置节，未写入')
            sections[current] = number
            continue
        entry = re.fullmatch(r'([ \t]*([^=\r\n]+?)[ \t]*=[ \t]*)(.*)', body)
        if current is None or not entry or not entry.group(2).strip():
            raise ValueError('剪映设置结构尚未验证，未写入')
        key = (current, entry.group(2).strip().casefold())
        if key in keys:
            raise ValueError('剪映设置含重复配置键，未写入')
        keys[key] = (number, entry.group(1), entry.group(3))
    return lines, sections, keys


def _edit_ini(data, key, value):
    if data is None:
        return ('[General]\r\n' + key + '=' + str(value).lower() + '\r\n').encode('utf-8'), None
    lines, sections, keys = _parse_ini(data)
    record = keys.get(('general', key.casefold()))
    before = None
    if record:
        number, prefix, previous = record
        if previous.casefold() not in ('true', 'false'):
            raise ValueError('剪映设置布尔值尚未验证，未写入：' + key)
        before = previous.casefold() == 'true'
        if before == value:
            return data, before
        ending = lines[number][len(lines[number].rstrip('\r\n')):]
        lines[number] = prefix + str(value).lower() + ending
    else:
        newline = next((re.search(r'\r\n|\n|\r', line).group() for line in lines
                        if re.search(r'\r\n|\n|\r', line)), '\r\n')
        if 'general' in sections:
            start = sections['general']
            position = next((number for number in sorted(sections.values()) if number > start), len(lines))
        else:
            position = len(lines)
            if lines and not lines[-1].endswith(('\r', '\n')):
                lines[-1] += newline
            lines.append('[General]' + newline)
            position += 1
        if position and not lines[position - 1].endswith(('\r', '\n')):
            lines[position - 1] += newline
        lines.insert(position, key + '=' + str(value).lower() + newline)
    bom = b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b''
    return bom + ''.join(lines).encode('utf-8'), before


def _profile_root(root):
    root = _no_links(root)
    if root.name.casefold() != 'user data' or root.parent.name.casefold() != 'jianyingpro':
        raise ValueError('仅支持剪映专业版的已有 JianyingPro/User Data 配置目录')
    config = _no_links(root / 'Config')
    if not root.is_dir() or not config.is_dir():
        raise ValueError('剪映尚未生成已有Config目录，未创建或猜测配置')
    anchor = _read_bytes(config / 'globalSetting', MAX_CONFIG_BYTES)
    _, sections, keys = _parse_ini(anchor)
    if 'general' not in sections or not any(section == 'general' for section, _ in keys):
        raise ValueError('尚未识别已有的正规globalSetting，未创建配置')
    return root


def _backup_original(path, data, backup_root):
    backup_root = _no_links(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    _no_links(backup_root)
    folder = backup_root / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
    folder.mkdir()
    record = {'schema': 1, 'source': str(path), 'existed': data is not None,
              'bytes': len(data) if data is not None else 0, 'sha256': _digest(data)}
    backup = folder / (path.name + '.bak') if data is not None else folder / '原文件不存在.json'
    if data is not None:
        with backup.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _digest(backup.read_bytes()) != record['sha256']:
            raise OSError('剪映设置备份SHA核验失败，未写入原配置')
    metadata = folder / '备份记录.json'
    metadata.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    if data is None:
        backup.write_bytes(metadata.read_bytes())
    if json.loads(metadata.read_text(encoding='utf-8')) != record or (data is None and backup.read_bytes() != metadata.read_bytes()):
        raise OSError('剪映设置备份记录核验失败，未写入原配置')
    return str(backup)


def _require_unchanged(path, expected, maximum):
    current = _read_bytes(path, maximum, optional=True)
    if _digest(current) != _digest(expected) or (current is None) != (expected is None):
        raise ValueError('设置已被其他程序更新，已保留备份且未覆盖，请稍后重试：' + str(path))


def _verify_readback(path, expected, maximum):
    actual = _read_bytes(path, maximum)
    if actual != expected:
        raise OSError('写入后设置回读核验失败，备份已保留：' + str(path))


def _atomic_replace(path, data, expected, maximum, *, replaced=lambda: None):
    _no_links(path)
    temporary = path.with_name('.editor-settings-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.read_bytes() != data:
            raise OSError('设置临时文件核验失败，未替换原配置')
        if expected is not None:
            mode = path.stat().st_mode
            if not mode & stat.S_IWUSR:
                raise PermissionError('设置文件为只读，未替换：' + str(path))
            os.chmod(temporary, stat.S_IMODE(mode))
        _no_links(path)
        _require_unchanged(path, expected, maximum)
        os.replace(temporary, path)
        replaced()
        _verify_readback(path, data, maximum)
    finally:
        if temporary.exists():
            temporary.unlink()


def _apply_one(root, backup_root, filename, key, value):
    path = _absolute(root) / 'Config' / filename
    result = {'ok': False, 'changed': False, 'value': value, 'path': str(path), 'backup': None}
    try:
        root = _profile_root(root)
        backup_root = _no_links(backup_root)
        if backup_root == root or root in backup_root.parents:
            raise ValueError('设置备份目录不能放在剪映User Data内部')
        old = _read_bytes(path, MAX_CONFIG_BYTES, optional=filename != 'globalSetting')
        edited, before = _edit_ini(old, key, value)
        result.update(before=before, before_exists=old is not None)
        if edited == old:
            result['ok'] = True
            return result
        with _advisory_lock(Path(str(path) + '.lock')):
            _profile_root(root)
            old = _read_bytes(path, MAX_CONFIG_BYTES, optional=filename != 'globalSetting')
            edited, before = _edit_ini(old, key, value)
            result.update(before=before, before_exists=old is not None)
            if edited == old:
                result['ok'] = True
                return result
            result['backup'] = _backup_original(path, old, backup_root)
            _atomic_replace(path, edited, old, MAX_CONFIG_BYTES,
                            replaced=lambda: result.update(changed=True))
            result['ok'] = True
    except Exception as exc:
        result['error'] = str(exc) or type(exc).__name__
    return result


def apply_update_policy(user_data_root, backup_root, disable_auto_update=True):
    """Apply beta first, then auto-update independently; return disk verification."""
    if type(disable_auto_update) is not bool:
        raise TypeError('disable_auto_update必须为bool')
    beta = _apply_one(user_data_root, backup_root, 'updateInfo', 'joinBeta', False)
    automatic = _apply_one(user_data_root, backup_root, 'globalSetting', 'enableAutoUpdate',
                           not disable_auto_update)
    return {'schema': 1, 'success': beta['ok'] and automatic['ok'],
            'disable_auto_update': disable_auto_update, 'beta': beta, 'auto_update': automatic,
            'scope': '仅核验磁盘设置；运行中剪映的内存状态需原生界面或重启核验'}


def editor_process_state():
    """Inspect the editor process during an initiated policy check."""
    result = {'ok': False, 'running': None, 'process_ids': []}
    if os.name != 'nt':
        result['error'] = '编辑器进程核验需要Windows'
        return result
    try:
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/tasklist.exe'
        process = subprocess.run([str(executable), '/FO', 'CSV', '/NH',
                                  '/FI', 'IMAGENAME eq JianyingPro.exe'],
                                 capture_output=True, timeout=5,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if process.returncode:
            raise OSError('进程列表读取失败')
        rows = csv.reader(process.stdout.decode('utf-8', errors='replace').splitlines())
        identifiers = [int(row[1]) for row in rows if len(row) > 1 and
                       row[0].casefold() == 'jianyingpro.exe' and row[1].isdigit()]
        result.update(ok=True, running=bool(identifiers), process_ids=identifiers)
    except Exception as exc:
        result['error'] = str(exc) or type(exc).__name__
    return result


def inspect_update_settings(user_data_root):
    """Read disk values and native reminder records without changing settings."""
    profile = _profile_root(user_data_root)
    files, reminder_records = {}, {}
    for filename, key in (('globalSetting', 'enableAutoUpdate'), ('updateInfo', 'joinBeta')):
        path = profile / 'Config' / filename
        item = {'path': str(path), 'key': key, 'ok': False, 'value': None}
        try:
            data = _read_bytes(path, MAX_CONFIG_BYTES)
            item.update(sha256=_digest(data), bytes=len(data),
                        modified_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
            _, _, keys = _parse_ini(data)
            entry = keys.get(('general', key.casefold()))
            value = entry[2].casefold() if entry else None
            if value in ('true', 'false'):
                item.update(ok=True, value=value == 'true')
            else:
                item['error'] = '磁盘配置缺少可核验的布尔值：' + key
            if filename == 'updateInfo':
                for (section, name), (_, _, raw) in keys.items():
                    match = re.fullmatch(r'(\d+\.\d+\.\d+(?:\.\d+)?)_(lasttimeshow|nonotify|checkednotify)', name)
                    if section == 'general' and match:
                        version, field = match.groups()
                        reminder_records.setdefault(version, {})[field] = raw
        except Exception as exc:
            item['error'] = str(exc) or type(exc).__name__
        files[filename] = item
    reminders = []
    for version, fields in sorted(reminder_records.items(), key=lambda row: tuple(map(int, row[0].split('.')))):
        reminders.append({'version': version,
                          'last_time_show': {'present': 'lasttimeshow' in fields, 'raw': fields.get('lasttimeshow')},
                          'nonotify': {'present': 'nonotify' in fields, 'raw': fields.get('nonotify')},
                          'checkednotify': {'present': 'checkednotify' in fields, 'raw': fields.get('checkednotify')}})
    return {'ok': all(item['ok'] for item in files.values()),
            'sampled_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'profile_root': str(profile), 'files': files,
            'version_reminders': reminders, 'reminder_suppression_verified': False,
            'reminder_record_interpretation': '仅记录磁盘原值；版本标记的布尔含义和弹窗效果尚未经过原生对照',
            'editor_process': editor_process_state()}


def attach_update_observation(report, user_data_root):
    """Keep supplementary reads independent of the two settings writes."""
    try:
        observation = inspect_update_settings(user_data_root)
    except Exception as exc:
        observation = {'ok': False, 'sampled_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       'profile_root': str(_absolute(user_data_root)),
                       'error': str(exc) or type(exc).__name__}
    report['disk_observation'] = observation
    matches = []
    for policy_key, filename in (('auto_update', 'globalSetting'), ('beta', 'updateInfo')):
        observed = observation.get('files', {}).get(filename, {})
        expected = report.get(policy_key, {}).get('value')
        if observed.get('ok') and type(expected) is bool:
            matches.append(observed.get('value') is expected)
    observation['matches_policy'] = all(matches) if len(matches) == 2 else None
    return report


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('偏好设置含重复JSON键，未覆盖')
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError('偏好设置含非法JSON数值，未覆盖')


def _read_preferences(path):
    """Strict shared reader for both saves and policy execution."""
    old = _read_bytes(path, MAX_PREFERENCES_BYTES, optional=True)
    try:
        current = {} if old is None else json.loads(old.decode('utf-8-sig'), object_pairs_hook=_json_object,
                                                    parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('偏好设置JSON损坏，已保留原文件，未覆盖') from exc
    if not isinstance(current, dict):
        raise ValueError('偏好设置不是JSON对象，未覆盖')
    return old, current


def apply_saved_update_policy(user_data_root, backup_root, preferences_path):
    """Serialize the latest saved choice and its disk-policy application.

    No captured UI bool is accepted: another window cannot save a new choice
    between our strict preference read and completing the editor settings writes.
    If that read/lock fails, beta is still independently disabled and auto-update
    remains explicitly unverified instead of guessing which choice was intended.
    """
    try:
        path = _no_links(preferences_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _no_links(path)
        with _advisory_lock(Path(str(path) + '.lock'), attempts=PREFERENCE_LOCK_ATTEMPTS):
            old, current = _read_preferences(path)
            report = apply_update_policy(user_data_root, backup_root, disables_auto_update(current))
            report.update(preference_verified=True, preferences_path=str(path),
                          preference_source='disk' if old is not None else 'default')
        return attach_update_observation(report, user_data_root)
    except Exception as exc:
        beta = _apply_one(user_data_root, backup_root, 'updateInfo', 'joinBeta', False)
        error = '未能核验最新保存的更新偏好，自动更新设置未改动：' + (str(exc) or type(exc).__name__)
        automatic = {'ok': False, 'changed': False, 'value': None,
                     'path': str(_absolute(user_data_root) / 'Config' / 'globalSetting'),
                     'backup': None, 'error': error}
        report = {'schema': 1, 'success': False, 'disable_auto_update': None,
                  'preference_verified': False, 'preferences_path': str(preferences_path),
                  'beta': beta, 'auto_update': automatic,
                  'scope': '偏好未核验；仅独立尝试关闭内测，未猜测自动更新选择'}
        return attach_update_observation(report, user_data_root)


def merge_preferences(path, values, explicit_disable=None):
    """Merge ordinary saves without erasing another window's explicit opt-out.

    Values for PREFERENCE in a stale UI snapshot are always ignored. Only a bool
    explicit_disable represents a fresh user choice; otherwise disk/default wins.
    Invalid settings, locks or concurrent uncooperative writes raise to the caller.
    """
    if not isinstance(values, dict):
        raise TypeError('values必须为dict')
    if explicit_disable is not None and type(explicit_disable) is not bool:
        raise TypeError('explicit_disable必须为bool或None')
    path = _no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _no_links(path)
    with _advisory_lock(Path(str(path) + '.lock'), attempts=PREFERENCE_LOCK_ATTEMPTS):
        old, current = _read_preferences(path)
        merged = dict(current)
        merged.update({key: value for key, value in values.items() if key != PREFERENCE})
        merged[PREFERENCE] = explicit_disable if type(explicit_disable) is bool else disables_auto_update(current)
        if old is not None and merged == current:
            return merged
        data = json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        if len(data) > MAX_PREFERENCES_BYTES:
            raise ValueError('偏好设置体积过大，未覆盖')
        _atomic_replace(path, data, old, MAX_PREFERENCES_BYTES)
        return merged
