#!/usr/bin/env python3
"""扫描工作树、暂存区及完整 Git 历史；只报告位置和规则，不回显值。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-key": re.compile(rb"\b(?:sk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    "credential-assignment": re.compile(rb'''(?i)(?:["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bduss|stoken|password|cookie)["']?\s*[:=]\s*["'])([A-Za-z0-9_+/%=.;:-]{24,})["']'''),
}
EXCLUDED_DIRS = {".git", "__pycache__", ".venv", ".pytest_cache", "dist", "build", "validation-local", "state", "receipts"}
LOCAL_NAMES = {"HANDOFF.md", "profiles.json", "installed-release.json", "RELEASE.json", "INSTALL.json"}


def scan(data, label, exact_values=()):
    found = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            found.append(dict(path=label, rule=name, line=data[:match.start()].count(b"\n")+1))
    for value in exact_values:
        encoded = value.encode()
        if len(encoded) >= 8 and encoded in data:
            found.append(dict(path=label, rule="local-credential-exact-match"))
    return found


def git(*args, root=ROOT, check=True):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and r.returncode:
        raise RuntimeError("Git query failed")
    return r


def local_values():
    from agentops.common import config_dir, credential, read_json
    p = config_dir()/"profiles.json"
    values = []
    if p.exists():
        for profile in read_json(p).get("profiles", {}).values():
            for field in ("credential_ref","web_credential_ref"):
                if profile.get(field):
                    try:
                        value=credential(profile[field])
                        values.append(value)
                        if profile[field].get("kind")=="sqlite-cookie":
                            values.extend(pair.split("=",1)[1] for pair in value.split("; ") if pair.startswith(("BDUSS=","STOKEN=")))
                    except Exception:
                        # 不可读凭据不是代码安全证明；结果另列覆盖数量。
                        pass
    return values


def check_tree(root, *, history=False, exact_values=()):
    root = Path(root)
    findings, checked, object_ids = [], 0, set()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.name in LOCAL_NAMES or path.suffix == ".pyc":
            continue
        findings.extend(scan(path.read_bytes(), str(rel), exact_values))
        checked += 1
    is_repo = git("rev-parse", "--is-inside-work-tree", root=root, check=False).returncode == 0
    staged = 0
    if is_repo:
        # 使用 blob ID 读暂存正文，避免工作树与实际提交不同。
        for record in git("ls-files", "--stage", "-z", root=root).stdout.split(b"\0"):
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            oid = meta.split()[1].decode()
            label = name.decode("utf-8", "replace")
            if Path(label).name in LOCAL_NAMES or label.startswith(("state/", "receipts/", "validation-local/")):
                findings.append(dict(path=label, rule="operational-file-staged"))
            findings.extend(scan(git("cat-file", "blob", oid, root=root).stdout, "index:"+label, exact_values))
            object_ids.add(oid)
            staged += 1
    historic = 0
    if history:
        if not is_repo:
            raise RuntimeError("Initialize Git before checking history")
        head = git("rev-parse", "--verify", "HEAD", root=root, check=False)
        if head.returncode == 0:
            for line in git("rev-list", "--objects", "--all", "--reflog", root=root).stdout.splitlines():
                parts = line.split(b" ", 1)
                oid = parts[0].decode()
                if oid in object_ids:
                    continue
                kind = git("cat-file", "-t", oid, root=root).stdout.strip()
                if kind != b"blob":
                    continue
                label = parts[1].decode("utf-8", "replace") if len(parts)>1 else oid
                if Path(label).name in LOCAL_NAMES or label.startswith(("state/", "receipts/", "validation-local/")):
                    findings.append(dict(path=label, rule="operational-file-in-history"))
                findings.extend(scan(git("cat-file", "blob", oid, root=root).stdout, "history:"+label, exact_values))
                object_ids.add(oid)
                historic += 1
    return dict(status="PASS" if not findings else "FAIL", worktree_files=checked,
                staged_files=staged, history_blobs=historic, history_requested=history,
                local_credentials_compared=len(exact_values), findings=findings)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--history", action="store_true")
    p.add_argument("--local-credentials", action="store_true")
    p.add_argument("--root", type=Path, default=ROOT)
    a = p.parse_args()
    try:
        r = check_tree(a.root, history=a.history, exact_values=local_values() if a.local_credentials else ())
    except Exception as e:
        r = dict(status="ERROR", exception=type(e).__name__, message="扫描未完成，不能发布。")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
