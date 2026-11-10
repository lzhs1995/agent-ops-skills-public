# Installation / 安裝

Use Python 3.11+ for the portable code. The current Windows installer additionally requires the maintained Python 3.14 executable at the location shown below; arbitrary Windows Python paths are not supported by this installer.

## macOS and compatible POSIX hosts

```sh
python3 -B tools/install.py
# Apply only after reviewing the preview:
python3 -B tools/install.py --apply
```

APFS compression is macOS-specific. A portable CLI or fixture test on Linux does not establish native disk or desktop support there.

## Windows PowerShell

```powershell
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -B tools\install.py
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -B tools\install.py --apply
```

When an existing CC Switch database is detected, the installer prepares managed skills. Registration is a separate step through its native API:

```powershell
& "$env:USERPROFILE\AppData\Local\Programs\Python\Python314\python.exe" -B tools\cc_switch_register.py
```

Review that preview before its `--apply`. A WSL distribution can be explicitly selected with `--wsl`; use the distribution actually installed on the machine.

## What apply changes / 套用會修改的內容

The installer creates a content-verified local release, managed CLI launchers, two skill entries, a local profile if absent, and bounded pointer blocks in supported agent instruction files. It preserves unrelated text, prior releases and an existing profile. Existing unmanaged paths are refused rather than overwritten.

套用會建立本機版本、命令入口、兩項技能連結，以及支援客戶端的提示區塊；既有設定、其他內容與舊版本保留。安裝不啟動背景清理，也不開始上傳。公開發布準備期間並未對真實家目錄執行此安裝。

Credentials are resolved locally from a configured reference or an existing client. Do not commit profiles, cookies, credential databases, upload IDs or receipts. `doctor --network` makes real network requests and is outside an offline installation check.
