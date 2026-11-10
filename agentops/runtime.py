"""把持久任务固定到创建它的完整版本；安装升级不迁移旧尝试。"""
from pathlib import Path
import sys

from .common import OpsError, read_json, sha256


def pin(kind):
    root = Path(__file__).resolve().parents[1]
    name = "baidu-netdisk-transfer" if kind == "netdisk" else "local-disk-maintenance"
    entry = root/"skills"/name/"scripts"/(kind+".py")
    manifest = root/"RELEASE.json"
    if manifest.exists():
        for relative, expected in read_json(manifest)["files"].items():
            if relative.startswith("agentops/") or relative == entry.relative_to(root).as_posix():
                if sha256(root/relative) != expected:
                    raise OpsError("RELEASE_CHANGED", "固定版本的执行文件发生变化。", path=str(root/relative))
    return dict(python=sys.executable, entry=str(entry), entry_sha256=sha256(entry),
                release=str(root), manifest_sha256=sha256(manifest) if manifest.exists() else None)


def check(task, kind):
    recorded = task.get("runtime")
    if recorded and recorded != pin(kind):
        raise OpsError("ORIGINAL_RUNTIME_REQUIRED", "请通过原任务固定的入口继续；新版本不能接管旧尝试。",
                       entry=recorded["entry"])
