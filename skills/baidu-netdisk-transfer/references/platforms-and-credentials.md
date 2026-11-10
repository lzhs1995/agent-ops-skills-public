# 本机入口与完整凭据

本机配置：用户主目录/.agents/local/agent-ops/profiles.json。
Mac状态：用户主目录/.local/state/agent-ops；Windows状态：
%LOCALAPPDATA%/agent-ops。安装回执记录脚本、解释器和不可变release的完整路径。
配置与状态不在Git仓库中；读取真实值后直接用于请求，不输出到普通日志。

支持的credential_ref：
- keychain：service、account，由security读取完整值。
- windows-credential：target，默认读取UTF-16LE通用凭据。
- file：完整本地JSON路径及fields数组，定位已有受保护配置中的字符串。
- environment：已有环境变量名。
- sqlite-cookie：现有客户端Cookie数据库绝对路径，以mode=ro读取当前可用登录，
  值仅在内存中用于百度请求；不复制或改写客户端数据库。数据库没有可读登录时用界面。

不必为每个agent复制一份密钥。同一OS用户的agent通过相同引用调用程序。
web_credential_ref和OpenAPI的credential_ref分别配置，不把Cookie或bdstoken当作access_token。
Mac凭据文件要求0600；Windows配置目录仅授予当前用户及SYSTEM访问。
存在凭据、接口可用、原生客户端登录分别验证。过期Token不覆盖或删掉；
仍可用的原生登录继续使用。只有确实缺少可用入口时才定位最小用户操作。

Windows使用已有Python314，不使用Microsoft Store重定向器。
WSL入口调用Windows原生Python、同一凭据与同一任务状态，转换本地/mnt/c路径。
本机配置、Cookie和登录会话不通过Git同步；两台各自保留登录，技能版本同步。

Windows/WSL 命令入口使用 UTF-8 输出。跨入 WSL 执行代码使用
`wsl.exe -d Ubuntu --exec ...`，避免默认 shell 提前展开 `$@` 或路径中的反斜杠。
若 DNS 返回 TUN 假地址而直连 TCP 超时，先检查现有本机 HTTP 代理。
确认可用后可在 baidu profile 的 `network.http_proxy` 指定其完整地址；该设置
只用于本网盘工具，不修改系统、Clash 或其他 agent 的代理。默认沿用已有环境。
Windows/WSL 共享同一份 Windows profile，避免建立第二份上传队列或凭据。

需要远程部署时先读取已安装cross-device-remote的SKILL.md，再运行xdev health；
使用push/run/detach/pull保留原job id并核SHA。超过60秒的工作用detach，
原任务迟到使用result，不重复派发。

