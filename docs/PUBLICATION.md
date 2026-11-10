# Publication notes / 發布說明

This source snapshot is published at [lzhs1995/agent-ops-skills-public](https://github.com/lzhs1995/agent-ops-skills-public). It is separate from the original private repository; the original private Git history is not included.

The original source snapshot did not include a `LICENSE` file. This export does not introduce MIT or another new license. No third-party source ownership restriction was identified in the bounded export review; this statement is not a full provenance audit.

此來源快照已發布至 [lzhs1995/agent-ops-skills-public](https://github.com/lzhs1995/agent-ops-skills-public)，與原私人儲存庫分開，不包含原私人 Git 歷史。

原來源快照沒有附上 `LICENSE`；本次匯出不新增 MIT 或其他授權條款。有限範圍的匯出審查未發現具體第三方來源歸屬限制，此結果不代表完整來源稽核。

## Usage and CI / 使用與 CI

Start with the [command interfaces](../README.md#try-the-command-interfaces), [skill entrypoints](../README.md#use-with-an-agent), and [installation guide](INSTALLATION.md). Downloading this repository does not automatically install skills or change installed runtimes. The installer defaults to a preview; transfers and cleanup require their own authorization.

The [Portable checks workflow](https://github.com/lzhs1995/agent-ops-skills-public/actions/workflows/test.yml) runs the secret check and isolated fixture suite on Linux and Windows. CI results do not establish live account access or automatic discovery in an agent client. See [agent compatibility](AGENT_COMPATIBILITY.md).

從[命令介面](../README.zh-TW.md#先檢查命令介面)、[技能入口](../README.zh-TW.md#跨-agent-使用)及[安裝說明](INSTALLATION.md)開始。下載儲存庫不會自動安裝技能或變更已安裝版本。安裝器預設為預覽；傳輸或清理需要個別授權。

[Portable checks 工作流程](https://github.com/lzhs1995/agent-ops-skills-public/actions/workflows/test.yml)在 Linux 與 Windows 上執行秘密檢查和隔離的合成測試。CI 結果不代表真實帳號可用，或 Agent 客戶端已自動載入技能；詳見[相容性說明](AGENT_COMPATIBILITY.md)。

## Excluded local material / 不包含的本機資料

Local audit reports, source provenance records, credentials, profiles, account identifiers, operational receipts and installed releases are excluded from this repository.

本機稽核報告、來源紀錄、憑據、設定、帳號識別資料、操作回執和已安裝版本不屬於公開內容。
