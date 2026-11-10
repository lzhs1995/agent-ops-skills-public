# Agent compatibility / 跨 Agent 相容性

Every agent uses the same two `SKILL.md` files and bundled Python entry points. No agent-specific copy of transfer state is created.

| Client / 客戶端 | Entry / 入口 | Evidence boundary / 驗證界線 |
| --- | --- | --- |
| Codex | Existing `.codex/skills` plus `AGENTS.md` pointer | Installer fixture coverage; no new live Codex registration in this export task. |
| Claude Code | Existing `.claude/skills` plus `CLAUDE.md` pointer | Installer fixture coverage; no new live Claude session launched. |
| Gemini CLI | Existing `.gemini/skills` plus `GEMINI.md` pointer | Installer fixture coverage; no new live Gemini session launched. |
| Other terminal agents | Read a selected `SKILL.md`; execute its Python script | Portable CLI help tested locally; discovery and permissions remain client-dependent. |
| Windows with CC Switch | Stage the two skills, then use `tools/cc_switch_register.py` | Requires the existing native manager API; preparation alone is not registration success. |
| WSL | Windows-native Python via the existing adapter | Shares the Windows profile and queue; this task does not claim a fresh WSL live test. |

## Agent handoff / Agent 交接

1. Read the selected skill and the original operation receipt, if any.
2. Keep its recorded runtime, task ID and source/destination binding.
3. Run the documented status or diagnostic operation before resuming work.
4. Preserve failed or unknown attempts. An entry-point installation is not evidence that an operation completed.

技能探索、命令可執行、帳號可用、操作完成是不同的驗證項目。不要將其中一項代替其他項目。

Optional desktop-control or remote-device tools mentioned in skill references are external integrations, not dependencies bundled here. Use an available authorized tool in the current agent environment; do not invent a missing endpoint.
