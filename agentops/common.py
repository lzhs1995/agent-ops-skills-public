"""凭据、原子回执和任务锁；只在调用时读取完整本地凭据。"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import platform
import stat
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class OpsError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self):
        return dict(status="ERROR", code=self.code, message=self.message, **self.details)


def now():
    return datetime.now(timezone.utc).isoformat()


def config_dir():
    return Path(os.environ.get("AGENT_OPS_CONFIG_DIR", str(Path.home() / ".agents/local/agent-ops")))


def state_dir():
    base = (Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            if os.name == "nt" else Path.home() / ".local/state")
    return Path(os.environ.get("AGENT_OPS_STATE_DIR", str(base / "agent-ops")))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def secure_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def atomic_json(path, value, *, exclusive=False):
    path = Path(path)
    secure_dir(path.parent)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if exclusive:
        with path.open("xb") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    else:
        fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sha256(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def identity(path):
    s = Path(path).lstat()
    return dict(dev=s.st_dev, ino=s.st_ino, size=s.st_size, mtime_ns=s.st_mtime_ns,
                mode=s.st_mode, nlink=s.st_nlink, flags=getattr(s, "st_flags", 0),
                allocated_bytes=getattr(s, "st_blocks", 0) * 512)


def unchanged(path, pin):
    current = identity(path)
    return all(current[k] == pin[k] for k in ("dev", "ino", "size", "mtime_ns", "mode", "nlink"))


def load_profile(name):
    path = config_dir() / "profiles.json"
    if not path.exists():
        raise OpsError("CONFIG_MISSING", "先运行安装器建立本机配置。", path=str(path))
    data = read_json(path)
    try:
        return data["profiles"][name]
    except KeyError:
        raise OpsError("PROFILE_MISSING", "本机没有这个配置。", profile=name)


def credential(ref):
    """返回完整凭据给调用脚本，不打印、不写入回执。"""
    kind = ref.get("kind")
    if kind == "keychain":
        if sys.platform != "darwin":
            raise OpsError("CREDENTIAL_PLATFORM", "该凭据属于 Mac Keychain。")
        p = subprocess.run(["security", "find-generic-password", "-s", ref["service"],
                            "-a", ref["account"], "-w"], capture_output=True, timeout=10)
        if p.returncode:
            raise OpsError("CREDENTIAL_UNAVAILABLE", "Keychain 项缺失、锁定或当前进程无权读取。")
        value = p.stdout.decode().strip()
    elif kind == "windows-credential":
        if os.name != "nt":
            raise OpsError("CREDENTIAL_PLATFORM", "请通过 Windows 原生 Python 读取凭据。")
        from ctypes import wintypes as W

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", W.DWORD), ("Type", W.DWORD), ("TargetName", W.LPWSTR),
                        ("Comment", W.LPWSTR), ("LastWritten", W.FILETIME),
                        ("CredentialBlobSize", W.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", W.DWORD), ("AttributeCount", W.DWORD),
                        ("Attributes", ctypes.c_void_p), ("TargetAlias", W.LPWSTR),
                        ("UserName", W.LPWSTR)]
        ptr = ctypes.POINTER(CREDENTIALW)()
        adv = ctypes.WinDLL("Advapi32", use_last_error=True)
        adv.CredReadW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        adv.CredReadW.restype = W.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        if not adv.CredReadW(ref["target"], 1, 0, ctypes.byref(ptr)):
            raise OpsError("CREDENTIAL_UNAVAILABLE", "Windows Credential Manager 项不可读。")
        try:
            value = ctypes.string_at(ptr.contents.CredentialBlob,
                                     ptr.contents.CredentialBlobSize).decode(ref.get("encoding", "utf-16-le")).strip()
        finally:
            adv.CredFree(ptr)
    elif kind == "file":
        path = Path(os.path.expandvars(ref["path"])).expanduser()
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise OpsError("CREDENTIAL_PERMISSIONS", "凭据文件应只允许当前用户读取。", path=str(path))
        value = read_json(path)
        for field in ref.get("fields", []):
            value = value[field]
        if not isinstance(value, str):
            raise OpsError("CREDENTIAL_FORMAT", "凭据引用没有定位到字符串。")
        value = value.strip()
    elif kind == "environment":
        value = os.environ.get(ref["name"], "").strip()
    elif kind == "sqlite-cookie":
        # 复用客户端已经保存的可读登录，不导出或改写 Cookie 数据库。
        path = Path(os.path.expandvars(ref["path"])).expanduser().resolve()
        con = sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=3)
        try:
            rows=con.execute("SELECT host_key,name,value FROM cookies WHERE value <> '' AND host_key IN ('.baidu.com','pan.baidu.com','.pan.baidu.com','pcs.baidu.com','.pcs.baidu.com')").fetchall()
        finally:
            con.close()
        chosen={}
        for host,name,item in sorted(rows,key=lambda r:0 if r[0] in ('.pan.baidu.com','pan.baidu.com') else 1 if r[0]=='.baidu.com' else 2):
            if isinstance(item,str) and item:
                chosen.setdefault(name,item)
        if not {'BDUSS','STOKEN'}.issubset(chosen):
            raise OpsError("CLIENT_LOGIN_UNAVAILABLE", "客户端没有可直接复用的登录；继续使用原生客户端界面。")
        value='; '.join(name+'='+item for name,item in chosen.items())
    else:
        raise OpsError("CREDENTIAL_KIND", "未配置可用的本机凭据引用。")
    if not value:
        raise OpsError("CREDENTIAL_EMPTY", "本机凭据为空。")
    return value


@contextlib.contextmanager
def task_lock(path):
    path = Path(path)
    secure_dir(path.parent)
    f = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            # Windows 区间锁也会禁止读取；先 stat，不能在竞争者加锁前读锁字节。
            if os.fstat(f.fileno()).st_size == 0:
                try:
                    f.write(b"0")
                    f.flush()
                except OSError:
                    raise OpsError("TASK_BUSY", "原任务仍持锁；不启动第二消费者。")
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise OpsError("TASK_BUSY", "原任务仍持锁；请读取进度，不要启动第二消费者。")
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise OpsError("TASK_BUSY", "原任务仍持锁；请读取进度，不要启动第二消费者。")
        yield
    finally:
        f.close()


def require_regular(path):
    path = Path(path).absolute()
    if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
        raise OpsError("UNSAFE_PATH", "仅处理不经过符号链接的普通文件。", path=str(path))
    return path


def cli_run(fn):
    # JSON 回执在 Windows/WSL 管道也统一 UTF-8，避免随系统代码页改变。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        value = fn()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except OpsError as e:
        print(json.dumps(e.as_dict(), ensure_ascii=False, indent=2))
        return 2
    except KeyboardInterrupt:
        print(json.dumps(dict(status="INTERRUPTED", message="保留原尝试；恢复时使用原任务目录。"), ensure_ascii=False))
        return 130
    except Exception as e:
        # urllib/进程异常可能包含 URL 查询中的凭据，不能直接输出 repr(e)。
        print(json.dumps(dict(status="ERROR", code="UNEXPECTED", exception=type(e).__name__,
                              message="原状态和原尝试保持；检查对应本地回执。"), ensure_ascii=False))
        return 1

