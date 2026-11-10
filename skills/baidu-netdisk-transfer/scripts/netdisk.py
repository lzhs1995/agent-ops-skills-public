#!/usr/bin/env python3
"""可直接运行的技能入口；已安装副本转到原子发布的完整release。"""
import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve()
pointer = here.parent.parent / "installed-release.json"
args = sys.argv[1:]
runtime = json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else None
wsl = os.name != "nt" and "microsoft" in __import__("platform").release().lower()
if wsl and runtime:
    py = subprocess.check_output(["wslpath", "-u", runtime["python"]], text=True).strip()
    for i in range(1,len(args)):
        if args[i-1] in {"--local","--task","--destination","--downloaded","--remote-evidence"}:
            if not __import__("re").match(r"^[A-Za-z]:[\\/]|^\\\\", args[i]):
                args[i] = subprocess.check_output(["wslpath","-w",str(Path(args[i]).expanduser().absolute())],text=True).strip()
    os.execv(py,[py,"-X","utf8","-B",runtime["entry"],*args])
if "--task" in args:
    task_file = Path(args[args.index("--task")+1])
    if task_file.is_file():
        runtime = json.loads(task_file.read_text(encoding="utf-8-sig")).get("runtime") or runtime
if runtime:
    original = Path(runtime["entry"])
    if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != runtime["entry_sha256"]:
        print('{"status":"ERROR","code":"ORIGINAL_ENTRY_CHANGED"}')
        raise SystemExit(2)
    if original.resolve() != here:
        os.execv(runtime["python"],[runtime["python"],"-X","utf8","-B",str(original),*args])
sys.path.insert(0,str(here.parent/"_runtime"))
for root in here.parents:
    if (root/"agentops").is_dir():
        sys.path.insert(0,str(root))
        break
from agentops.netdisk import main
raise SystemExit(main())

