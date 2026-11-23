"""Isolate the installed editor's optional native codec in a child process.

Only the public exported ABI is used. No editor binaries are distributed.
Every native result is JSON checked. A child has a finite request lifetime,
so native std::string allocations cannot accumulate in the GUI process.
"""
from __future__ import annotations
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

DEC = '?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z'
ENC = '?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBV34@@Z'
ENABLE = '?enable@EncryptUtils@lvve@@QEAAX_N@Z'
MAX_BYTES = 128 * 1024 * 1024

class CodecError(RuntimeError):
    pass

class _String(ctypes.Structure):
    _fields_ = [('data', ctypes.c_ubyte * 16), ('size', ctypes.c_uint64), ('capacity', ctypes.c_uint64)]

def _string(value: str):
    raw = value.encode('utf-8')
    result = _String()
    result.size = len(raw)
    keepalive = None
    if len(raw) < 16:
        result.capacity = 15
        ctypes.memmove(result.data, raw, len(raw))
    else:
        keepalive = ctypes.create_string_buffer(raw)
        ctypes.c_void_p.from_buffer(result).value = ctypes.addressof(keepalive)
        result.capacity = len(raw)
    return result, keepalive

def _value(value):
    if value.size > MAX_BYTES or value.size > value.capacity:
        raise CodecError('原生组件返回了无效字符串长度')
    address = ctypes.addressof(value) if value.capacity < 16 else ctypes.c_void_p.from_buffer(value).value
    if not address and value.size:
        raise CodecError('原生组件返回空指针')
    return ctypes.string_at(address, value.size).decode('utf-8')

class _Native:
    def __init__(self, app_dir):
        if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise CodecError('加密草稿需要 Windows x64')
        self.cookie = os.add_dll_directory(str(app_dir))
        os.environ['PATH'] = str(app_dir) + os.pathsep + os.environ.get('PATH', '')
        os.chdir(app_dir)
        self.lib = ctypes.WinDLL(str(Path(app_dir) / 'videoeditor.dll'))
        pointer = ctypes.POINTER(_String)
        self.dec = ctypes.WINFUNCTYPE(pointer, ctypes.c_void_p, pointer, pointer, pointer, ctypes.POINTER(ctypes.c_bool))((DEC, self.lib))
        self.enc = ctypes.WINFUNCTYPE(pointer, ctypes.c_void_p, pointer, pointer)((ENC, self.lib))
        self.enable = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)((ENABLE, self.lib))

    def decrypt(self, text):
        source, keepalive = _string(text)
        options, options_keepalive = _string('{}')
        result, ok = _String(), ctypes.c_bool(False)
        self.dec(None, ctypes.byref(result), ctypes.byref(source), ctypes.byref(options), ctypes.byref(ok))
        if not ok.value:
            raise CodecError('此版本草稿无法解密；未修改源文件')
        plain = _value(result)
        if not plain:
            raise CodecError('剪映组件未授权外部进程读取此加密草稿（当前版本可能限制草稿访问）。请使用剪映内复制与原生导出流程。')
        json.loads(plain)
        return plain

    def encrypt(self, text):
        json.loads(text)
        source, keepalive = _string(text)
        result = _String()
        self.enable(None, True)
        self.enc(None, ctypes.byref(result), ctypes.byref(source))
        cipher = _value(result)
        if self.decrypt(cipher) != text:
            raise CodecError('草稿加密往返校验失败')
        return cipher

def worker(app_dir, request_path, result_path):
    try:
        codec = _Native(Path(app_dir).resolve())
        request = json.loads(Path(request_path).read_text('utf-8'))
        if request['op'] not in ('decrypt', 'encrypt'):
            raise ValueError('Invalid codec operation')
        result = getattr(codec, request['op'])(request['text'])
        payload = {'ok': True, 'text': result}
    except Exception as exc:
        payload = {'ok': False, 'error': str(exc)}
    Path(result_path).write_text(json.dumps(payload, ensure_ascii=True), 'utf-8')

class Codec:
    def __init__(self, app_dir=None):
        self.app_dir = Path(app_dir).resolve() if app_dir else None
        self.process = None
        self.lock = threading.Lock()

    def close(self):
        if self.process:
            self.process.kill()
            self.process.wait(timeout=5)
            self.process = None

    def _call(self, op, text):
        if not self.app_dir:
            raise CodecError('请先选择包含 videoeditor.dll 的剪映版本目录')
        with self.lock, tempfile.TemporaryDirectory(prefix='jyexport_codec_') as temp:
            request_path, result_path = Path(temp) / 'request.json', Path(temp) / 'result.json'
            request_path.write_text(json.dumps({'op': op, 'text': text}, ensure_ascii=True), 'utf-8')
            entry = Path(__file__).resolve().parents[1] / 'main.py'
            if not getattr(sys, 'frozen', False) and not entry.is_file():
                entry = Path(__file__).resolve().parents[1] / 'delivery.py'
                if not entry.is_file():
                    raise CodecError('缺少独立读取进程入口，请保持工具或skill目录完整')
            args = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(entry)]
            args.extend(['--codec-worker', str(self.app_dir), str(request_path), str(result_path)])
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                returncode = self.process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                self.close()
                raise CodecError('原生草稿组件超时；已终止独立读取进程')
            self.process = None
            if returncode or not result_path.is_file():
                raise CodecError('本机剪映组件不兼容或读取进程退出；未修改源文件')
            payload = json.loads(result_path.read_text('utf-8'))
            if not payload.get('ok'):
                raise CodecError(payload.get('error', '草稿组件失败'))
            return payload['text']

    def read(self, path):
        raw = Path(path).read_text(encoding='utf-8-sig')
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('草稿根节点必须是对象')
            return data
        except json.JSONDecodeError:
            return json.loads(self._call('decrypt', raw))

    def encode(self, data, encrypted):
        text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        return self._call('encrypt', text) if encrypted else text

    @staticmethod
    def encrypted(path):
        return not Path(path).read_bytes().lstrip(b'\xef\xbb\xbf \r\n\t').startswith(b'{')
