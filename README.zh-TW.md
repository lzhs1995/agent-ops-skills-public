<p align="center"><img src="assets/logo.svg" width="128" height="128" alt="Agent Ops Skills 標誌"></p>

# Agent Ops Skills

[English](README.md) · [跨 Agent 相容性](docs/AGENT_COMPATIBILITY.md) · [安裝說明](docs/INSTALLATION.md)

提供兩項可攜技能：百度網盤傳輸驗證與有界的本機磁碟維護。需要 Python 3.11 以上；核心僅使用 Python 標準函式庫。

| 技能 | 功能 |
| --- | --- |
| [baidu-netdisk-transfer](skills/baidu-netdisk-transfer/SKILL.md) | 規劃傳輸、沿原嘗試續傳，並以完整雜湊驗證獨立回下載。 |
| [local-disk-maintenance](skills/local-disk-maintenance/SKILL.md) | 檢查容量、產生有限清單，驗證已授權的清理或 APFS 壓縮。 |

## 先檢查命令介面

在儲存庫根目錄執行下列命令，只顯示說明或安裝預覽，不會安裝技能、上傳或清理：

```sh
python3 -B skills/baidu-netdisk-transfer/scripts/netdisk.py --help
python3 -B skills/local-disk-maintenance/scripts/disk.py --help
python3 -B tools/install.py
```

安裝器預設為預覽。使用 `--apply` 前，請先閱讀[安裝會修改的項目](docs/INSTALLATION.md)。現有憑據、設定、執行版本與操作回執保留在本機；安裝技能不代表授權上傳或清理。

## 跨 Agent 使用

讓 Agent 讀取所需技能的 `SKILL.md`，再透過隨附腳本操作。安裝器管理現有 Codex、Claude Code、Gemini 的技能目錄，以及共用 `.agents/skills` 入口。其他 Agent 可使用相同 Markdown 與命令列；未經測試的客戶端不宣稱支援自動探索。

[相容性說明](docs/AGENT_COMPATIBILITY.md)分開列出命令列能力、安裝器涵蓋範圍與真實客戶端驗證。此套件本身不需要網路服務或 MCP 伺服器。

## 驗證來源

```sh
python3 -B -m unittest discover -s tests -v
python3 -B tools/secret_check.py --history
```

沒有 Git 歷史的匯出目錄請使用 `tools/secret_check.py --root .`。測試使用隔離的合成檔案；真實傳輸仍需個別授權的帳號、精確路徑與獨立回下載驗證，測試通過不能代替真實帳號驗收。

## 發布狀態

**已準備的來源快照。** 目標儲存庫：[lzhs1995/agent-ops-skills-public](https://github.com/lzhs1995/agent-ops-skills-public)。

原來源快照沒有附上 `LICENSE` 檔案，本次匯出未新增授權條款。準備這些檔案不會建立或推送儲存庫、變更已安裝版本，也不代表授權傳輸或清理；未攜帶原私人 Git 歷史、裝置設定或操作回執。詳見[發布說明](docs/PUBLICATION.md)。
