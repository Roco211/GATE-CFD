"""Encrypt credentials using Windows DPAPI or a protected deployment key."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import stat
import tempfile


class CredentialStore:
    def __init__(self, path: str | Path, *, key_file: str | Path | None = None):
        self.path = Path(path)
        configured = key_file or os.getenv('GRID_CREDENTIAL_KEY_FILE')
        self.key_file = Path(configured) if configured else None

    @property
    def available(self) -> bool:
        return os.name == 'nt' or self.key_file is not None

    @property
    def protection(self) -> str:
        return 'fernet-file-key' if self.key_file else 'windows-dpapi-user'

    def _server_cipher(self):
        from cryptography.fernet import Fernet
        try:
            if self.key_file is None or self.key_file.is_symlink():
                raise ValueError()
            descriptor = os.open(self.key_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or (os.name != 'nt' and metadata.st_mode & 0o077):
                    raise ValueError()
                return Fernet(source.read(128).strip())
        except (OSError, ValueError):
            raise ValueError('服务器加密密钥不可用，请检查部署密钥及访问权限。') from None

    def _crypt(self, payload: bytes, decrypt: bool = False) -> bytes:
        if self.key_file is not None:
            cipher = self._server_cipher()
            return cipher.decrypt(payload) if decrypt else cipher.encrypt(payload)
        if not self.available:
            raise ValueError('后台未配置加密存储；可取消记住密钥，仅在后台内存中使用。')

        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(payload)
        source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        method.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # UI_FORBIDDEN keeps unattended startup from opening operating-system prompts.
        if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError('无法读取本机加密密钥，请在页面重新配置。' if decrypt else 'Windows 加密保存失败，原配置未更改。')
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))

    def load(self) -> tuple[str, str] | None:
        if not self.path.exists():
            return None
        try:
            record = json.loads(self.path.read_text(encoding='utf-8'))
            expected_version = 2 if self.key_file else 1
            if record.get('version') != expected_version or record.get('protection') != self.protection:
                raise ValueError()
            raw = self._crypt(base64.b64decode(record['ciphertext'], validate=True), decrypt=True)
            credentials = json.loads(raw)
            if not all(isinstance(credentials.get(k), str) and credentials[k] for k in ('key', 'secret')):
                raise ValueError()
            return credentials['key'], credentials['secret']
        except Exception:
            raise ValueError('无法读取本机加密密钥，请在页面重新配置。') from None

    def save(self, key: str, secret: str) -> None:
        raw = json.dumps({'key': key, 'secret': secret}, separators=(',', ':')).encode()
        ciphertext = base64.b64encode(self._crypt(raw)).decode('ascii')
        record = {'version': 2 if self.key_file else 1, 'protection': self.protection, 'ciphertext': ciphertext}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.credentials-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(json.dumps(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
