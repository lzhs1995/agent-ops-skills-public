# Operating principles / 操作原則

- Bind every operation to an exact source, destination and durable receipt.
- Preserve the original attempt and runtime when resuming.
- Treat remote metadata, a completed upload response and an independently verified download as separate evidence.
- Prepare cleanup as a bounded list; preserve files that changed or remain open.
- Report observed capacity separately from space attributable to the operation.
- Keep credentials and machine-specific operational records outside source control.

每次操作都需精確範圍與可追溯回執。續傳沿用原嘗試，清理依有限清單執行；未知結果不改寫為成功。本文件僅記錄通用方法，不收錄私人研究、帳號或事件記錄。
