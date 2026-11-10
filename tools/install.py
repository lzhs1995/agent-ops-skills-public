#!/usr/bin/env python3
"""安装完整不可变版本、本地配置与仅两个技能的发现入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agentops.common import OpsError, atomic_json, cli_run, now, read_json, secure_dir, sha256, task_lock

NAMES = {"baidu-netdisk-transfer": "netdisk", "local-disk-maintenance": "disk"}
BEGIN, END = "<!-- agent-ops-skills:start -->", "<!-- agent-ops-skills:end -->"
OWNER = "agent-ops-skills"


def source_files(root=ROOT):
    files = []
    for folder in ("agentops", "skills", "tools", "tests", ".github"):
        for p in (root/folder).rglob("*"):
            if p.is_file() and not any(x in ("__pycache__", ".DS_Store") for x in p.parts) and p.suffix != ".pyc":
                if p.is_symlink():
                    raise OpsError("SOURCE_SYMLINK", "发布源不能通过符号链接引入本机配置。")
                files.append(p.relative_to(root).as_posix())
    files += [name for name in ("README.md", "AGENTS.md", "pyproject.toml", ".gitignore") if (root/name).exists()]
    return sorted(files)


def release_manifest(root=ROOT):
    files = {name: sha256(root/name) for name in source_files(root)}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return dict(version="0.1.0", content_sha256=digest, files=files)


def verify_release(root, manifest):
    for name, expected in manifest["files"].items():
        p = root/name
        if not p.is_file() or p.is_symlink() or sha256(p) != expected:
            raise OpsError("RELEASE_CHANGED", "安装版本完整性检查失败。", path=str(p))


def stage_release(source, base):
    manifest = release_manifest(source)
    secure_dir(base)
    release = base/(manifest["version"]+"-"+manifest["content_sha256"][:16])
    if release.exists():
        if read_json(release/"RELEASE.json") != manifest:
            raise OpsError("RELEASE_CHANGED", "已有同名版本的清单不匹配。")
        verify_release(release, manifest)
        return release, manifest
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
    for name in manifest["files"]:
        dest = stage/name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/name, dest)
    atomic_json(stage/"RELEASE.json", manifest, exclusive=True)
    verify_release(stage, manifest)
    os.rename(stage, release)
    return release, manifest


def layout(home):
    win = os.name == "nt"
    base = Path(os.environ.get("LOCALAPPDATA", str(home/"AppData/Local"))) if win else home/".local"
    return dict(config=home/".agents/local/agent-ops", state=base/"agent-ops" if win else base/"state/agent-ops",
                releases=base/"agent-ops/releases" if win else base/"share/agent-ops/releases", bin=home/".local/bin")


def ensure_private_windows(directory):
    if os.name != "nt":
        return
    import csv
    result = subprocess.check_output(["whoami.exe", "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader([result.strip()]))[-1]
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise OpsError("WINDOWS_SID_UNKNOWN", "无法确认当前用户 SID，未修改权限。")
    p = subprocess.run(["icacls.exe", str(directory), "/inheritance:r", "/grant:r",
                        "*"+sid+":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True)
    if p.returncode:
        raise OpsError("CONFIG_ACL_FAILED", "本机配置目录权限未确认。")


def default_profile(home, keychain_service=None, keychain_account=None, client_root=None, cookie_db=None):
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", str(home/"AppData/Local")))
        roaming = Path(os.environ.get("APPDATA", str(home/"AppData/Roaming")))
        candidates = [roaming/"baidu/BaiduNetdisk", local/"BaiduNetdisk"]
        caches = [home/"AppData/Local/pip/Cache", home/"AppData/Local/npm-cache/_cacache"]
    else:
        candidates = [home/"Library/Containers/com.baidu.netdisk/Data/Library/Application Support/com.baidu.netdisk",
                      home/"Library/Application Support/com.baidu.netdisk",
                      home/"Library/Containers/com.baidu.netdisk-mac/Data/Library/Application Support/com.baidu.netdisk"]
        caches = [home/"Library/Caches/pip", home/".npm/_cacache", home/"Library/Caches/Homebrew/downloads"]
    client = client_root or next((str(p) for p in candidates if p.exists()), None)
    baidu = dict(backend="auto")
    if client:
        baidu["client_root"] = client
    cookies = ([Path(cookie_db)] if cookie_db else [])
    if os.name!="nt":
        cookies += [home/"Library/Containers/com.baidu.netdisk/Data/Library/Application Support/baidunetdisk/Cookies"]
    else:
        for root in candidates:
            cookies += [root/"Cookies",root/"Network/Cookies"]
    existing_cookie=next((p for p in cookies if p.is_file()),None)
    if existing_cookie:
        baidu["web_credential_ref"]=dict(kind="sqlite-cookie",path=str(existing_cookie.resolve()))
    if keychain_service and keychain_account and sys.platform == "darwin":
        probe = subprocess.run(["security", "find-generic-password", "-s", keychain_service,
                                "-a", keychain_account], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if probe.returncode == 0:
            baidu["credential_ref"] = dict(kind="keychain", service=keychain_service, account=keychain_account)
    protected = [home/p for p in ("Documents", "Desktop", "Downloads", ".ollama", ".lmstudio", ".codex", ".claude", ".agents", ".cc-switch")]
    return dict(version=1, profiles=dict(baidu=baidu, disk=dict(cache_roots=[str(p) for p in caches if p.exists()],
                 protected_paths=[str(p) for p in protected], compression_exclusions=[str(p) for p in protected])))


def save_managed_text(path, text, receipt_dir, mode=0o600):
    path = path.resolve() if path.is_symlink() else path
    old = path.read_bytes() if path.exists() else None
    data = text.encode("utf-8")
    if old == data:
        return dict(path=str(path), changed=False, sha256=hashlib.sha256(data).hexdigest())
    backup = None
    if old is not None:
        backup = receipt_dir/(hashlib.sha256(str(path).encode()).hexdigest()[:20]+".before")
        backup.write_bytes(old)
        mode = path.stat().st_mode & 0o777
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".agent-ops-", dir=path.parent)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if (path.read_bytes() if path.exists() else None) != old:
        raise OpsError("CONCURRENT_EDIT", "入口在安装期间变化；保留临时文件与备份。", path=str(path))
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    return dict(path=str(path), changed=True, sha256=sha256(path), backup=str(backup) if backup else None)


def add_block(path, body, evidence):
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    block = BEGIN+"\n"+body.strip()+"\n"+END
    if BEGIN in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise OpsError("AMBIGUOUS_ENTRY", "共享指令存在重复安装标记，保留原文件。")
        original = re.sub(re.escape(BEGIN)+r".*?"+re.escape(END), lambda _:block, original, flags=re.S)
    else:
        original = original.rstrip()+"\n\n"+block+"\n"
    return save_managed_text(path, original, evidence)


def mac_link(path, target, releases):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        if path.resolve() == target.resolve():
            return
        if not path.resolve().is_relative_to(releases.resolve()):
            raise OpsError("ENTRY_OWNED_ELSEWHERE", "已有同名技能不是本安装器管理，保留原入口。", path=str(path))
    elif path.exists():
        raise OpsError("ENTRY_OWNED_ELSEWHERE", "已有同名技能目录，保留原目录。", path=str(path))
    temporary = path.with_name("."+path.name+"-"+uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, path)


def copied_skill(source, target, python, entry, releases, evidence):
    pointer = target/"installed-release.json"
    if target.exists():
        if target.is_symlink() or not pointer.is_file():
            raise OpsError("ENTRY_OWNED_ELSEWHERE", "已有技能受其他管理器控制。", path=str(target))
        previous = read_json(pointer)
        if previous.get("owner") != OWNER:
            raise OpsError("ENTRY_OWNED_ELSEWHERE", "已有目录的所有者不同。", path=str(target))
        if previous.get("entry") == str(entry):
            return
        shutil.move(str(target), evidence/(target.name+"-previous"))
    shutil.copytree(source, target)
    atomic_json(pointer, dict(owner=OWNER, python=python, entry=str(entry),
                             entry_sha256=sha256(entry)), exclusive=True)


def install(home, *, apply=False, keychain_service=None, keychain_account=None, client_root=None,
            cookie_db=None):
    paths = layout(home)
    manifest = release_manifest()
    manager = "cc-switch" if os.name=="nt" and (home/".cc-switch/cc-switch.db").is_file() else "local"
    if not apply:
        return dict(status="PREVIEW", manager=manager, skills=list(NAMES), manifest=manifest["content_sha256"],
                    paths={k:str(v) for k,v in paths.items()}, background_cleanup=False)
    for key in ("config", "state", "releases"):
        secure_dir(paths[key])
    ensure_private_windows(paths["config"])
    ensure_private_windows(paths["state"])
    with task_lock(paths["state"]/"install.lock"):
        stamp = uuid.uuid4().hex
        evidence = secure_dir(paths["state"]/"installs"/stamp)
        release, manifest = stage_release(ROOT, paths["releases"])
        profile = paths["config"]/"profiles.json"
        if not profile.exists():
            atomic_json(profile, default_profile(home, keychain_service, keychain_account, client_root,cookie_db), exclusive=True)
        python = sys.executable
        if os.name=="nt":
            required = home/"AppData/Local/Programs/Python/Python314/python.exe"
            if Path(python).resolve() != required.resolve():
                raise OpsError("WINDOWS_PYTHON_PATH", "安装须使用本机维护的 Python314。", required=str(required))
        entries = []
        for name, command in NAMES.items():
            source = release/"skills"/name
            entry = source/"scripts"/(command+".py")
            if manager == "cc-switch":
                # 新技能先进入管理器的未登记扫描范围；后续由原生 API 选择性导入。
                target = home/".agents/skills"/name
                if not target.exists():
                    copied_skill(source, target, python, entry, paths["releases"], evidence)
                elif not (target/"installed-release.json").is_file() or read_json(target/"installed-release.json").get("entry") != str(entry):
                    raise OpsError("MANAGED_UPDATE_REQUIRED", "技能已由 CC Switch 接管；通过其原生更新流程更新。", path=str(target), release=str(release))
                entries.append(str(target))
            else:
                for client in (".agents", ".codex", ".claude", ".gemini"):
                    if client != ".agents" and not (home/client).exists():
                        continue
                    target = home/client/"skills"/name
                    if os.name == "nt":
                        copied_skill(source, target, python, entry, paths["releases"], evidence)
                    else:
                        mac_link(target, source, paths["releases"])
                    entries.append(str(target))
            paths["bin"].mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                launcher = paths["bin"]/("agent-"+command+".cmd")
                content = '@echo off\r\nREM agent-ops-skills managed launcher\r\n"'+python+'" -X utf8 -B "'+str(entry)+'" %*\r\n'
            else:
                launcher = paths["bin"]/("agent-"+command)
                content = "#!/bin/sh\n# agent-ops-skills managed launcher\nexec "+shlex.quote(python)+" -B "+shlex.quote(str(entry))+" \"$@\"\n"
            if launcher.exists() and "agent-ops-skills managed launcher" not in launcher.read_text():
                raise OpsError("LAUNCHER_OWNED_ELSEWHERE", "已有同名命令，保留原入口。", path=str(launcher))
            save_managed_text(launcher, content, evidence, 0o755)
        shared = home/".agents/AGENTS.shared.md"
        body = ("百度网盘连接、传输和断点核验使用 baidu-netdisk-transfer；磁盘告警、缓存治理和空间验收使用 local-disk-maintenance。\n"
                f"技能入口：{home/'.agents/skills'}/<技能名>/SKILL.md。完整本地配置：{profile}；由脚本读取凭据。\n"
                "先读原任务状态；继续使用原版本、原断点和原回执。安装不授权新清理，不重放已结束批次。")
        policy = [add_block(shared, body, evidence)]
        # Windows/WSL 的客户端提示词由 CC Switch 管理；共享文件足够，不改其生成产物。
        if manager != "cc-switch":
            for client, filename in ((".codex","AGENTS.md"),(".claude","CLAUDE.md"),(".gemini","GEMINI.md")):
                if (home/client).exists():
                    policy.append(add_block(home/client/filename,
                        f"百度传输与磁盘治理的共用技能入口见 {shared}；按任务读取对应 SKILL.md。", evidence))
        result = dict(status="CC_SWITCH_IMPORT_READY" if manager=="cc-switch" else "INSTALLED", at=now(),
                      manager=manager, release=str(release), release_sha256=manifest["content_sha256"],
                      python=python, skills=entries, config=str(profile), config_preserved=True,
                      launchers=str(paths["bin"]), policy=policy, old_releases_preserved=True,
                      background_cleanup=False, network_uploads=0, receipt=str(evidence/"INSTALL.json"))
        atomic_json(evidence/"INSTALL.json", result, exclusive=True)
        atomic_json(paths["state"]/"current-install.json", result)
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--keychain-service")
    p.add_argument("--keychain-account")
    p.add_argument("--client-root")
    p.add_argument("--cookie-db",help="现有客户端可读 Cookie 数据库，仅记录引用，不导出值")
    a = p.parse_args()
    return cli_run(lambda: install(Path.home(), apply=a.apply, keychain_service=a.keychain_service,
                                  keychain_account=a.keychain_account, client_root=a.client_root,cookie_db=a.cookie_db))


if __name__ == "__main__":
    raise SystemExit(main())
