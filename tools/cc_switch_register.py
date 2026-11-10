#!/usr/bin/env python3
"""通过现有 CC Switch 原生 API 仅登记这两个技能；不写活动数据库。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agentops.common import OpsError, atomic_json, cli_run, now, read_json, secure_dir, sha256, state_dir, task_lock

NAMES={"baidu-netdisk-transfer":"netdisk","local-disk-maintenance":"disk"}


def wsl_entry(distro):
    # 技能目录本身由 CC Switch 投影。本段仅建立两个命令，沿安装指针转 Windows。
    script=r'''
import json,os,pathlib,shlex,tempfile
h=pathlib.Path.home(); rows=[]
for name,command in {'baidu-netdisk-transfer':'netdisk','local-disk-maintenance':'disk'}.items():
    entry=h/'.agents/skills'/name/'scripts'/(command+'.py')
    pointer=entry.parent.parent/'installed-release.json'
    if not entry.is_file() or not pointer.is_file():
        raise RuntimeError('CC Switch skill projection is not ready: '+name)
    p=h/'.local/bin'/('agent-'+command)
    p.parent.mkdir(parents=True,exist_ok=True)
    text='#!/bin/sh\n# agent-ops-skills managed launcher\nexec /usr/bin/python3 -B '+shlex.quote(str(entry))+' "$@"\n'
    text=text.replace('\\n','\n')
    old=p.read_text() if p.exists() else None
    if old is not None and 'agent-ops-skills managed launcher' not in old:
        raise RuntimeError('Existing command belongs to another installer')
    if old!=text:
        if old is not None:
            backup=h/'.local/state/agent-ops/install-backups'; backup.mkdir(parents=True,exist_ok=True)
            (backup/(p.name+'-'+str(__import__('time').time_ns()))).write_text(old)
        fd,tmp=tempfile.mkstemp(prefix='.agent-ops-',dir=p.parent)
        with os.fdopen(fd,'w') as f: f.write(text)
        os.chmod(tmp,0o755); os.replace(tmp,p)
    rows.append({'command':str(p),'entry':str(entry),'python':json.loads(pointer.read_text())['python']})
print(json.dumps({'status':'WSL_ENTRY_READY','home':str(h),'entries':rows}))
'''
    p=subprocess.run(["wsl.exe","-d",distro,"--exec","python3","-B","-c",script],capture_output=True,timeout=45)
    if p.returncode:
        raise OpsError("WSL_ENTRY_UNAVAILABLE","WSL 技能投影或命令尚未就绪；保留原生登记，不重新导入。")
    return json.loads(p.stdout.decode("utf-8"))


def register(apply=False,wsl=None):
    if os.name!="nt":
        raise OpsError("WINDOWS_ONLY","CC Switch 登记在 Windows 本机执行。")
    home=Path.home()
    adapter=home/".cc-switch/wsl-sync/scripts/native_tools.py"
    if not apply:
        return dict(status="PREVIEW",skills=list(NAMES),adapter=str(adapter),native_database_writes=False)
    if not adapter.is_file():
        raise OpsError("NATIVE_ADAPTER_MISSING","未找到现役 CC Switch 原生 API 适配器。")
    current=read_json(state_dir()/"current-install.json")
    for name in NAMES:
        pointer=home/".agents/skills"/name/"installed-release.json"
        if not pointer.is_file():
            raise OpsError("SKILL_NOT_STAGED","先运行 install.py --apply。")
    spec=importlib.util.spec_from_file_location("agent_ops_cc_native",adapter)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with task_lock(state_dir()/"cc-switch-register.lock"):
        evidence=secure_dir(state_dir()/"installs"/("cc-switch-"+uuid.uuid4().hex))
        with module.Native() as native:
            before=native.call("get_installed_skills")
            atomic_json(evidence/"SKILLS_BEFORE.json",before,exclusive=True)
            own=[r for r in before if r["directory"] in NAMES]
            unknown=[name for name in NAMES if name not in {r["directory"] for r in own}]
            for row in own:
                if not all(row.get("apps",{}).get(app) for app in ("claude","codex","gemini")):
                    raise OpsError("MANAGED_ENABLEMENT_DIFFERS","已有同名技能的启用设置不同，保留原记录。")
            if unknown:
                imports=[dict(directory=name,apps=dict(claude=True,codex=True,gemini=True,grokbuild=False)) for name in unknown]
                atomic_json(evidence/"NATIVE_IMPORT_INTENT.json",dict(at=now(),imports=imports,adapter_sha256=sha256(adapter)),exclusive=True)
                try:
                    response=native.call("import_skills_from_apps",{"imports":imports})
                    atomic_json(evidence/"NATIVE_IMPORT_RESPONSE.json",response,exclusive=True)
                except Exception:
                    # 提交可能已经完成；下一步只读核收，不发第二次导入。
                    atomic_json(evidence/"NATIVE_IMPORT_UNKNOWN.json",dict(at=now(),replayed=False),exclusive=True)
            after=native.call("get_installed_skills")
            atomic_json(evidence/"SKILLS_AFTER.json",after,exclusive=True)
        own_after=[r for r in after if r["directory"] in NAMES]
        if len(own_after)!=2 or not all(all(r.get("apps",{}).get(a) for a in ("claude","codex","gemini")) for r in own_after):
            raise OpsError("NATIVE_IMPORT_NOT_CONFIRMED","原生登记未完整确认；先读原次回执，不重放。",evidence=str(evidence))
        old_other={r["id"]:r for r in before if r["directory"] not in NAMES}
        new_other={r["id"]:r for r in after if r["directory"] not in NAMES}
        if old_other!=new_other:
            raise OpsError("UNRELATED_SKILLS_CHANGED","并发期间其他技能记录发生变化；保留原快照，需独立核对。",evidence=str(evidence))
        options=home/".cc-switch/wsl-sync/settings.json"
        old=options.read_bytes()
        data=json.loads(old)
        portable=set(data.get("portable_skills",[]))
        if not set(NAMES).issubset(portable):
            (evidence/"sync-settings.before.json").write_bytes(old)
            data["portable_skills"]=sorted(portable|set(NAMES))
            if options.read_bytes()!=old:
                raise OpsError("SYNC_OPTIONS_CHANGED","同步设置在读取后变化；保留原登记，未覆盖设置。")
            atomic_json(options,data)
        wsl_result=None
        if wsl:
            deadline=time.monotonic()+50
            while True:
                try:
                    wsl_result=wsl_entry(wsl)
                    break
                except OpsError:
                    if time.monotonic()>=deadline:
                        raise
                    time.sleep(2)
        result=dict(status="REGISTERED",at=now(),release=current["release"],skills=own_after,
                    unrelated_records_unchanged=True,native_database_direct_writes=0,wsl=wsl_result,
                    evidence=str(evidence),runtime_install=current["receipt"])
        atomic_json(evidence/"RESULT.json",result,exclusive=True)
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply",action="store_true")
    p.add_argument("--wsl")
    a=p.parse_args()
    return cli_run(lambda:register(a.apply,a.wsl))


if __name__=="__main__":
    raise SystemExit(main())
