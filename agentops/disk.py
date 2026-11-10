"""固定清单磁盘治理；目标、保留范围与原回执均属于本轮任务。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import plistlib
import shutil
import stat
import struct
import subprocess
import time
import uuid
from pathlib import Path

from .common import (OpsError, atomic_json, cli_run, identity, load_profile, now,
                     read_json, require_regular, secure_dir, sha256, state_dir,
                     task_lock, unchanged)
from . import runtime

GIB = 1024**3
ARCHIVES_MODELS = {".zip", ".7z", ".rar", ".gguf", ".safetensors", ".wbk"}


def capacity(path=None, details=False):
    volume = str(Path(path or ("/System/Volumes/Data" if platform.system() == "Darwin"
                              else Path.home())).resolve())
    if platform.system() == "Darwin":
        s = os.statvfs(volume)
        # diskutil 不接受任意子目录；先以 df 精确解析它所在的设备。
        df = subprocess.check_output(["df", "-P", volume], text=True, timeout=10)
        device = df.splitlines()[-1].split()[0]
        p = subprocess.run(["diskutil", "info", "-plist", device], capture_output=True, check=True, timeout=15)
        apfs = plistlib.loads(p.stdout)
        if "APFSContainerFree" not in apfs:
            raise OpsError("NOT_APFS", "没有取得 APFS 容器实际可用量。")
        fs_free, container_free = s.f_bavail*s.f_frsize, apfs["APFSContainerFree"]
        result = dict(statvfs_available_bytes=fs_free, apfs_container_free_bytes=container_free,
                      available_bytes=min(fs_free, container_free))
        result["swap"] = subprocess.check_output(["sysctl", "vm.swapusage"], text=True, timeout=10).strip()
        if details:
            result["memory_observation"] = subprocess.check_output(
                ["top", "-l", "1", "-n", "10", "-o", "mem", "-stats", "pid,command,mem,cmprs,cpu"],
                text=True, timeout=25)
    elif os.name == "nt":
        from ctypes import wintypes as W
        available, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDiskFreeSpaceExW.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                             ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
        if not kernel.GetDiskFreeSpaceExW(volume, ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)):
            raise OpsError("CAPACITY_UNAVAILABLE", "Windows 实际可用量读取失败。")
        result = dict(available_bytes=available.value, total_free_bytes=free.value, total_bytes=total.value)
        if details:
            class Performance(ctypes.Structure):
                _fields_ = [("cb",W.DWORD)]+[(name,ctypes.c_size_t) for name in (
                    "CommitTotal","CommitLimit","CommitPeak","PhysicalTotal","PhysicalAvailable",
                    "SystemCache","KernelTotal","KernelPaged","KernelNonpaged","PageSize")]+[
                    ("HandleCount",W.DWORD),("ProcessCount",W.DWORD),("ThreadCount",W.DWORD)]
            perf=Performance()
            perf.cb=ctypes.sizeof(perf)
            psapi=ctypes.WinDLL("psapi",use_last_error=True)
            psapi.GetPerformanceInfo.argtypes=[ctypes.POINTER(Performance),W.DWORD]
            psapi.GetPerformanceInfo.restype=W.BOOL
            if psapi.GetPerformanceInfo(ctypes.byref(perf),perf.cb):
                result["memory"]={name+"_bytes":getattr(perf,name)*perf.PageSize for name in
                    ("CommitTotal","CommitLimit","CommitPeak","PhysicalTotal","PhysicalAvailable")}
            else:
                result["memory"]={"status":"UNAVAILABLE"}
            script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize,CurrentUsage,PeakUsage | ConvertTo-Json -Compress"
            result["pagefile"] = subprocess.check_output(["powershell.exe", "-NoProfile", "-Command", script],
                                                         text=True, encoding="utf-8", timeout=20).strip()
    else:
        s = os.statvfs(volume)
        result = dict(available_bytes=s.f_bavail*s.f_frsize, diagnostic_only=True)
    result.update(at=now(), volume=volume, platform=platform.system(),
                  available_GiB=result["available_bytes"]/GIB)
    return result


def private_bytes(path):
    if platform.system() != "Darwin":
        return None

    class AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                    ("common", ctypes.c_uint32), ("volume", ctypes.c_uint32),
                    ("directory", ctypes.c_uint32), ("file", ctypes.c_uint32), ("fork", ctypes.c_uint32)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getattrlist.argtypes = [ctypes.c_char_p, ctypes.POINTER(AttrList), ctypes.c_void_p,
                                ctypes.c_size_t, ctypes.c_ulong]
    libc.getattrlist.restype = ctypes.c_int
    attrs, out = AttrList(5, 0, 0x80000000, 0, 0, 0, 8), ctypes.create_string_buffer(64)
    if libc.getattrlist(os.fsencode(path), ctypes.byref(attrs), out, len(out), 0x21):
        raise OpsError("PRIVATE_SIZE_UNKNOWN", "无法确认该文件系统独占量。")
    fields = struct.unpack_from("=6I", out.raw)
    if fields != (32, 0x80000000, 0, 0, 0, 8):
        raise OpsError("PRIVATE_SIZE_UNKNOWN", "系统未返回预期独占量属性。")
    return struct.unpack_from("=Q", out.raw, 24)[0]


class CompressionCapacity:
    """只在无损压缩批次内复用最多1秒的容量观察；收尾始终重新测量。"""
    def __init__(self, volume):
        self.volume = volume
        self.sample = None
        self.checked = None

    def __call__(self, force=False):
        if force or self.sample is None or time.monotonic()-self.checked >= 1:
            sample = capacity(self.volume)
            self.sample = sample
            self.checked = time.monotonic()
        return self.sample


def mac_xattrs(path):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    raw = os.fsencode(path)
    size = libc.listxattr(raw, None, 0, 1)
    if size < 0:
        raise OpsError("XATTR_UNAVAILABLE", "无法完整读取文件扩展属性。")
    names = ctypes.create_string_buffer(size or 1)
    count = libc.listxattr(raw, names, size, 1)
    if count < 0:
        raise OpsError("XATTR_UNAVAILABLE", "无法完整读取文件扩展属性。")
    result = {}
    for name in names.raw[:count].split(b"\0"):
        if not name:
            continue
        n = libc.getxattr(raw, name, None, 0, 0, 1)
        if n < 0:
            raise OpsError("XATTR_UNAVAILABLE", "无法完整读取文件扩展属性。")
        data = ctypes.create_string_buffer(n or 1)
        if libc.getxattr(raw, name, data, n, 0, 1) != n:
            raise OpsError("XATTR_CHANGED", "读取期间文件扩展属性发生变化。")
        result[os.fsdecode(name)] = data.raw[:n]
    return result


def open_inodes(out):
    if platform.system() != "Darwin":
        return set()
    control = Path(out) / "lsof-positive-control"
    control.touch(exist_ok=True)
    with control.open("rb"):
        r = subprocess.run(["lsof", "-n", "-P", "-u", str(os.geteuid()), "-F", "pfcDatin0"],
                           capture_output=True, timeout=25)
    if r.returncode or r.stderr.strip():
        raise OpsError("OPEN_HANDLES_UNKNOWN", "无法可靠核实活动句柄，停止本轮变更。")
    inodes, dev, ino = set(), None, None
    for raw in r.stdout.split(b"\0"):
        field = raw.lstrip(b"\n").decode("utf-8", "replace")
        if field.startswith("f"):
            dev = ino = None
        elif field.startswith("D"):
            dev = int(field[1:], 16)
        elif field.startswith("i") and field[1:].isdigit():
            ino = int(field[1:])
        if dev is not None and ino is not None:
            inodes.add((dev, ino))
    pin = identity(control)
    if (pin["dev"], pin["ino"]) not in inodes:
        raise OpsError("OPEN_HANDLES_CONTROL", "活动句柄检查的阳性对照缺失。")
    return inodes


def contained(path, roots):
    return any(path.is_relative_to(Path(os.path.expandvars(r)).expanduser().resolve()) for r in roots)


def walk_bounded(roots, max_entries=10000, max_depth=6):
    stack = [(Path(r), 0) for r in roots]
    count = 0
    while stack:
        directory, depth = stack.pop()
        if not directory.is_dir():
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > max_entries:
                        raise OpsError("SCAN_LIMIT", "已达到本轮有限扫描上限。", visited=max_entries)
                    if entry.is_symlink():
                        continue
                    p = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if depth < max_depth and entry.name not in (".git", "node_modules", ".claude", ".codex"):
                            stack.append((p, depth+1))
                    elif entry.is_file(follow_symlinks=False):
                        yield p
        except PermissionError:
            continue


def plan_files(profile, kind, roots, target_gib=None, max_entries=10000, age_days=7):
    if kind == "apfs" and platform.system() != "Darwin":
        raise OpsError("APFS_ONLY", "研究冷文件压缩仅在已验证的 Mac/APFS 路径启用。")
    if not roots:
        if kind == "apfs":
            raise OpsError("EXPLICIT_ROOT_REQUIRED", "冷文件压缩必须指定本轮已授权的目录。")
        roots = profile.get("cache_roots", [])
    roots = [Path(os.path.expandvars(str(p))).expanduser().absolute() for p in roots]
    for root in roots:
        if root.resolve() != root:
            raise OpsError("SYMLINK_ROOT", "不经符号链接或目录联接清理。", path=str(root))
        if kind == "cache" and not contained(root, profile.get("cache_roots", [])):
            raise OpsError("CACHE_ROOT_NOT_ALLOWED", "该目录未被本机配置确认是可再生缓存。", path=str(root))
    rows, skipped, seen, partial = [], {}, set(), False
    try:
        for p in walk_bounded(roots, max_entries):
            reason = None
            pin = identity(p)
            protected = profile.get("compression_exclusions" if kind=="apfs" else "protected_paths", [])
            if contained(p, protected):
                reason = "protected"
            elif p.suffix.lower() in ARCHIVES_MODELS or pin["nlink"] != 1 or pin["flags"] != 0:
                reason = "archive_model_links_or_flags"
            elif time.time_ns()-pin["mtime_ns"] < age_days*86400*10**9:
                reason = "recent"
            elif pin["size"] <= 0 or kind == "apfs" and pin["size"] > 2_000_000_000:
                reason = "size"
            elif (pin["dev"], pin["ino"]) in seen:
                reason = "duplicate_inode"
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            if kind == "apfs":
                try:
                    private = private_bytes(p)
                except OpsError:
                    skipped["private_size_unknown"] = skipped.get("private_size_unknown", 0)+1
                    continue
                if private < pin["allocated_bytes"]*0.9:
                    skipped["shared_extents"] = skipped.get("shared_extents", 0)+1
                    continue
            else:
                private = None
            digest = sha256(p)
            if not unchanged(p, pin):
                skipped["changed"] = skipped.get("changed", 0)+1
                continue
            rows.append(dict(path=str(p), identity=pin, sha256=digest, private_bytes=private))
            seen.add((pin["dev"], pin["ino"]))
    except OpsError as e:
        if e.code != "SCAN_LIMIT":
            raise
        partial = True
    tool = shutil.which("afsctool") if kind == "apfs" else None
    if kind == "apfs" and not tool:
        raise OpsError("COMPRESSION_TOOL_MISSING", "本机未找到经过验证的 afsctool。")
    if len({r["identity"]["dev"] for r in rows}) > 1:
        raise OpsError("MULTIPLE_VOLUMES", "不同磁盘应分别生成清单、分别验收。")
    volume = str(Path(rows[0]["path"]).parent) if rows else str(Path.home())
    return dict(version=1, task_id=uuid.uuid4().hex, at=now(), kind=kind, volume=volume,
                platform=platform.system(), roots=[str(r) for r in roots], files=rows, runtime=runtime.pin("disk"),
                target_bytes=int(target_gib*GIB) if target_gib is not None else None,
                age_days=age_days, scan_partial=partial, skipped=skipped,
                source_deletion=kind=="cache", compression_tool=tool,
                compression_tool_sha256=sha256(tool) if tool else None,
                profile_sha256=hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest())


def windows_delete_closed(row):
    from ctypes import wintypes as W
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
                                  W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.SetFileInformationByHandle.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    kernel.SetFileInformationByHandle.restype = W.BOOL
    handle = kernel.CreateFileW(row["path"], 0x80000000 | 0x10000, 0, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OpsError("OPEN_OR_LOCKED", "Windows 无法独占打开该缓存；保留原文件。")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as f:
        s = os.fstat(f.fileno())
        pin = row["identity"]
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) != (pin["dev"], pin["ino"], pin["size"], pin["mtime_ns"]):
            raise OpsError("SOURCE_CHANGED", "Windows 独占句柄与清单身份不一致。")
        if hashlib.file_digest(f, "sha256").hexdigest() != row["sha256"]:
            raise OpsError("SOURCE_CHANGED", "缓存内容发生变化。")
        delete = ctypes.c_ubyte(1)
        if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise OpsError("DELETE_FAILED", "Windows 未确认删除该独占缓存句柄。")


def compress_one(row, episode, tool, capacity_observer=None):
    path = Path(row["path"])
    before = identity(path)
    observed = (capacity_observer(force=before["size"] >= 64*1024**2)
                if capacity_observer is not None else capacity(path.parent))
    if before["size"] > 2_000_000_000 or observed["available_bytes"] < 8*GIB:
        raise OpsError("COMPRESSION_HEADROOM", "文件超过已验证大小或临时空间不足。")
    attrs = mac_xattrs(path)
    if {"com.apple.ResourceFork", "com.apple.decmpfs"} & attrs.keys():
        raise OpsError("SPECIAL_FORK", "该文件已有资源叉或压缩属性，保留原状。")
    if private_bytes(path) < before["allocated_bytes"]*0.9:
        raise OpsError("SHARED_EXTENTS", "文件数据块当前共享，保留原状。")
    backup = episode / "original.apfs-clone"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    libc.clonefile.restype = ctypes.c_int
    if libc.clonefile(os.fsencode(path), os.fsencode(backup), 1):
        raise OpsError("CLONE_FAILED", "无法创建独立回退克隆。")
    if sha256(backup) != row["sha256"]:
        raise OpsError("CLONE_MISMATCH", "回退克隆完整字节不匹配。")
    atomic_json(episode / "NATIVE_INTENT.json", dict(at=now(), tool_sha256=sha256(tool),
                source=str(path), backup=str(backup), automatic_retries=0), exclusive=True)
    r = subprocess.run([tool, "-c", "-j2", "-T", "LZVN", str(path)], capture_output=True, timeout=180)
    atomic_json(episode / "TOOL.json", dict(returncode=r.returncode, stdout=r.stdout.decode("utf-8","replace"),
                stderr=r.stderr.decode("utf-8","replace")), exclusive=True)
    if r.returncode:
        raise OpsError("COMPRESSION_FAILED", "压缩未完成；原回退克隆保留，禁止自动重试。")
    if sha256(path) != row["sha256"]:
        raise OpsError("HASH_MISMATCH", "压缩后完整字节不匹配；保留回退克隆。")
    os.utime(path, ns=(path.stat().st_atime_ns, before["mtime_ns"]))
    after = identity(path)
    if not all(before[k] == after[k] for k in ("dev","ino","size","mode","nlink","mtime_ns")):
        raise OpsError("METADATA_CHANGED", "压缩后文件身份或必要属性不一致。")
    new_attrs = mac_xattrs(path)
    if any(new_attrs.get(k) != v for k, v in attrs.items() if k != "com.apple.provenance") \
            or set(new_attrs)-set(attrs)-{"com.apple.provenance","com.apple.ResourceFork","com.apple.decmpfs"}:
        raise OpsError("XATTR_CHANGED", "压缩后非系统管理属性发生变化。")
    if after["flags"] not in (0, stat.UF_COMPRESSED):
        raise OpsError("FLAGS_CHANGED", "压缩后存在非预期标志。")
    with path.open("rb") as f:
        os.fsync(f.fileno())
    backup.unlink()
    return dict(state="COMPRESSED_VERIFIED" if after["flags"] else "UNCHANGED_VERIFIED",
                sha256=row["sha256"], before=before, after=after, content_identical=True,
                metadata_preserved=True, temporary_clone_removed=True, source_deleted=False,
                os_managed_provenance_changed=attrs.get("com.apple.provenance")!=new_attrs.get("com.apple.provenance"),
                allocated_reduction_not_volume_release=before["allocated_bytes"]-after["allocated_bytes"])


def apply_plan(plan_path, profile, authorization):
    plan_path = Path(plan_path)
    plan = read_json(plan_path)
    runtime.check(plan, "disk")
    if not authorization.strip():
        raise OpsError("AUTHORIZATION_REQUIRED", "记录当前已获授权的范围，不重复询问已有授权。")
    if plan["platform"] != platform.system():
        raise OpsError("PLATFORM_CHANGED", "该清单属于另一平台。")
    if plan["profile_sha256"] != hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest():
        raise OpsError("PROFILE_CHANGED", "保留清单或本机配置变化，需重新预览。")
    out = plan_path.parent / (plan["task_id"] + "-execution")
    with task_lock(state_dir() / "disk" / "controller.lock"):
        if out.exists():
            raise OpsError("ATTEMPT_EXISTS", "原执行目录已存在；先核收原回执，禁止重放。", path=str(out))
        secure_dir(out)
        initial = capacity(plan["volume"])
        atomic_json(out/"START.json", dict(at=now(), authorization=authorization,
                    plan_sha256=sha256(plan_path), initial_capacity=initial, native_retries=0), exclusive=True)
        opened = open_inodes(out)
        checked = time.monotonic()
        receipts, failed, stop_reason = [], False, None
        compression_capacity = CompressionCapacity(plan["volume"]) if plan["kind"] == "apfs" else None
        last_progress = time.monotonic()
        if plan["kind"] == "apfs" and sha256(plan["compression_tool"]) != plan["compression_tool_sha256"]:
            raise OpsError("TOOL_CHANGED", "原压缩工具版本变化，禁止用新工具重放旧清单。")
        for row in plan["files"]:
            if (out/"STOP_AFTER_CURRENT").exists():
                stop_reason = "operator_stop"
                break
            observed = compression_capacity() if compression_capacity is not None else capacity(plan["volume"])
            if plan["target_bytes"] and observed["available_bytes"] >= plan["target_bytes"]:
                stop_reason = "target_reached"
                break
            p = Path(row["path"])
            if time.monotonic()-checked > 30:
                opened = open_inodes(out)
                checked = time.monotonic()
            key = hashlib.sha256(os.fsencode(p)).hexdigest()[:24]
            episode = secure_dir(out/key)
            try:
                require_regular(p)
                if not contained(p, plan["roots"]) or (plan["kind"]=="cache" and not contained(p, profile.get("cache_roots",[]))):
                    raise OpsError("SCOPE_MISMATCH", "文件不属于固定根目录和当前缓存许可范围。")
                if not unchanged(p, row["identity"]) or sha256(p) != row["sha256"]:
                    raise OpsError("SOURCE_CHANGED", "文件与固定清单不同，保留原件。")
                if (row["identity"]["dev"], row["identity"]["ino"]) in opened:
                    raise OpsError("OPEN_OR_LOCKED", "文件存在活动句柄，保留原件。")
                if contained(p, profile.get("compression_exclusions" if plan["kind"]=="apfs" else "protected_paths", [])):
                    raise OpsError("PROTECTED", "文件属于保留范围。")
                atomic_json(episode/"INTENT.json", dict(at=now(), source=str(p), **row), exclusive=True)
                if os.name == "nt":
                    if plan["kind"] != "cache":
                        raise OpsError("APFS_ONLY", "Windows 不执行 APFS 压缩。")
                    windows_delete_closed(row)
                    result = dict(state="CACHE_DELETED", sha256=row["sha256"], source_deleted=True)
                elif platform.system() == "Darwin":
                    if plan["kind"] == "apfs":
                        # afsctool 自己锁定源文件；父进程持同一 inode 的 flock
                        # 会让子进程 EAGAIN，即使它返回 0 也没有发生压缩。
                        # 保留全局控制锁、活动句柄检查、前后完整 SHA 与回退克隆。
                        if not unchanged(p, row["identity"]):
                            raise OpsError("SOURCE_CHANGED", "原生压缩前文件发生变化。")
                        result = compress_one(row, episode, plan["compression_tool"], compression_capacity)
                    else:
                        import fcntl
                        with p.open("rb") as f:
                            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            if not unchanged(p, row["identity"]):
                                raise OpsError("SOURCE_CHANGED", "持锁时文件发生变化。")
                            p.unlink()
                            result = dict(state="CACHE_DELETED", sha256=row["sha256"], source_deleted=True)
                else:
                    raise OpsError("UNSUPPORTED_MUTATION", "此平台仅支持只读诊断。")
                result.update(source=str(p), at=now())
            except OpsError as e:
                result = dict(state="SKIPPED" if e.code in ("SOURCE_CHANGED","OPEN_OR_LOCKED","PROTECTED",
                              "SPECIAL_FORK","SHARED_EXTENTS") else "FAILED", source=str(p), **e.as_dict())
                if (episode/"NATIVE_INTENT.json").exists():
                    result["state"] = "FAILED"
                failed = result["state"] == "FAILED"
            except Exception as e:
                result = dict(state="FAILED", source=str(p), exception=type(e).__name__)
                failed = True
            atomic_json(episode/"RESULT.json", result, exclusive=True)
            receipts.append(dict(path=str(episode/"RESULT.json"), sha256=sha256(episode/"RESULT.json"),
                                 state=result["state"]))
            if time.monotonic()-last_progress >= 5:
                atomic_json(out/"PROGRESS.json", dict(at=now(), stage="processing_fixed_plan",
                            considered=len(receipts), total=len(plan["files"]), last_source=str(p),
                            last_state=result["state"], capacity_observation=observed))
                last_progress = time.monotonic()
            if failed:
                atomic_json(out/"STOP.json", dict(at=now(), reason="operation_or_integrity_failure",
                            automatic_retry=False), exclusive=True)
                break
            if plan["kind"] == "apfs" and len(receipts) == 2:
                gain = sum(read_json(item["path"]).get("allocated_reduction_not_volume_release", 0)
                           for item in receipts)
                if gain <= 0:
                    stop_reason = "pilot_no_file_allocation_gain"
                    atomic_json(out/"STOP.json", dict(at=now(), reason=stop_reason, automatic_retry=False),
                                exclusive=True)
                    break
        final = capacity(plan["volume"])
        result = dict(at=now(), status="STOPPED" if failed else "FINITE_SCOPE_COMPLETED",
                      plan=str(plan_path), volume=plan["volume"], receipts=receipts, considered=len(receipts),
                      runtime=plan.get("runtime"),
                      total=len(plan["files"]), initial_capacity=initial, final_capacity=final,
                      target_bytes=plan["target_bytes"],
                      target_met=final["available_bytes"]>=plan["target_bytes"] if plan["target_bytes"] else None,
                      volume_delta_attribution="Concurrent whole-volume change, not solely this operation.",
                      native_retries=0, stop_reason=stop_reason, further_cleanup_planned=False)
        if stop_reason and stop_reason != "target_reached":
            result["status"] = "STOPPED"
        atomic_json(out/"SUMMARY.json", result, exclusive=True)
        atomic_json(out/"PROGRESS.json", dict(at=now(), stage="terminal", status=result["status"],
                    considered=len(receipts), total=len(plan["files"]), capacity_observation=final,
                    summary=str(out/"SUMMARY.json")))
        return dict(status=result["status"], considered=result["considered"], total=result["total"],
                    summary=str(out/"SUMMARY.json"), target_met=result["target_met"])


def verify(summary_path, interval=2):
    result = read_json(summary_path)
    runtime.check(result, "disk")
    for item in result["receipts"]:
        if sha256(item["path"]) != item["sha256"]:
            raise OpsError("RECEIPT_CHANGED", "原文件回执发生变化。")
    samples = [capacity(result["volume"])]
    time.sleep(interval)
    samples.append(capacity(result["volume"]))
    target = result["target_bytes"]
    record = dict(at=now(), receipt_hashes_pass=True, samples=samples,
                  minimum_available_bytes=min(s["available_bytes"] for s in samples),
                  target_met=all(s["available_bytes"]>=target for s in samples) if target else None,
                  operation_status=result["status"])
    output = Path(summary_path).parent / ("VERIFY-"+str(time.time_ns())+".json")
    atomic_json(output, record, exclusive=True)
    return dict(**record, evidence=str(output))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="disk")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--path", type=Path)
    c.add_argument("--details", action="store_true")
    c.add_argument("--target-gib", type=float)
    c = sub.add_parser("plan")
    c.add_argument("--kind", choices=("cache","apfs"), default="cache")
    c.add_argument("--root", action="append", default=[])
    c.add_argument("--target-gib", type=float)
    c.add_argument("--max-entries", type=int, default=10000)
    c.add_argument("--age-days", type=float, default=7)
    c.add_argument("--out", type=Path)
    c = sub.add_parser("apply")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--authorization", required=True)
    c = sub.add_parser("verify")
    c.add_argument("--summary", type=Path, required=True)
    args = p.parse_args()

    def action():
        if args.command == "check":
            r = capacity(args.path, args.details)
            if args.target_gib is not None:
                r.update(target_bytes=int(args.target_gib*GIB),
                         target_met=r["available_bytes"]>=args.target_gib*GIB)
            return r
        if args.command == "verify":
            return verify(args.summary)
        profile = load_profile(args.profile)
        if args.command == "apply":
            return apply_plan(args.plan, profile, args.authorization)
        if args.age_days < 0 or not 1 <= args.max_entries <= 100000:
            raise OpsError("PLAN_BOUNDS", "必须使用非负冷却时间和有界扫描规模。")
        r = plan_files(profile, args.kind, args.root, args.target_gib, args.max_entries, args.age_days)
        output = args.out or state_dir()/"disk"/r["task_id"]/"PLAN.json"
        atomic_json(output, r, exclusive=True)
        return dict(status="PREVIEW", plan=str(output), candidates=len(r["files"]),
                    scan_partial=r["scan_partial"], skipped=r["skipped"], source_deletion=r["source_deletion"],
                    logical_bytes=sum(f["identity"]["size"] for f in r["files"]))
    return cli_run(action)


if __name__ == "__main__":
    raise SystemExit(main())

