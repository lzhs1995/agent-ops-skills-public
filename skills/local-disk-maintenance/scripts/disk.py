#!/usr/bin/env python3
"""可直接运行的技能入口；Windows与WSL共用同一本机配置和回执。"""
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
        if args[i-1] in {"--root","--plan","--summary","--path","--out"}:
            if not __import__("re").match(r"^[A-Za-z]:[\\/]|^\\\\", args[i]):
                args[i] = subprocess.check_output(["wslpath","-w",str(Path(args[i]).expanduser().absolute())],text=True).strip()
    os.execv(py,[py,"-X","utf8","-B",runtime["entry"],*args])
for flag in ("--plan", "--summary"):
    if flag in args:
        saved = Path(args[args.index(flag)+1])
        if saved.is_file():
            runtime = json.loads(saved.read_text(encoding="utf-8-sig")).get("runtime") or runtime
            break
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
from agentops.disk import main
raise SystemExit(main())

