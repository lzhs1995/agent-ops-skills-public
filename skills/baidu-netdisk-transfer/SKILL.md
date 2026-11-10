---
name: baidu-netdisk-transfer
description: 在 Mac 或 Windows 使用已有百度网盘登录与本地凭据，诊断连接、规划上传、原任务断点续传、下载及完整字节验真。用于百度网盘连不上、大文件上传、分片停滞、迁移与云端往返核验。
---

# 百度网盘传输

先读本机配置，再使用本 skill 的 scripts/netdisk.py；已安装命令为 agent-netdisk。
默认配置名 baidu。完整凭据由程序读取，不让用户每次找密钥。配置不在 Git
仓库内，位置与 Windows/WSL 入口见 references/platforms-and-credentials.md。

## 先确定当前入口与原任务

1. 执行 agent-netdisk doctor --network，分别核凭据可读、接口实际可用和原生客户端。
   token 可读但返回过期，不等于登录客户端不可用。CDP 9223 拒绝连接只否定 CDP。
2. 已有 task.json 时，先读状态、原 runtime 和原句柄，沿原任务恢复；不新建 uploadid
   作为连通性测试，不重启共享客户端，不启动第二个 drain/watch。
3. 新任务执行 plan，明确源文件和精确远端文件路径。auto 优先使用验证通过的
   OpenAPI；已有客户端登录可读且真实接口通过时，小文件可走 web 通道。
   web v1只接≤256MiB、4MiB分片、≤64片；这是保守工作范围，不是百度官方上限。
   更大文件交给已登录原生客户端。不能把本地缓存行当作远端存在证明。

~~~text
agent-netdisk doctor --network
agent-netdisk plan --local 完整本地路径 --remote /精确目录/文件名 --backend auto
agent-netdisk upload --task 返回的task.json
agent-netdisk resume --task 原task.json
agent-netdisk status --task 原task.json
agent-netdisk download --task 原task.json --destination 独立下载文件路径
agent-netdisk verify --task 原task.json --downloaded 独立下载文件路径
~~~

## 上传前必须算清楚

分片接口、账号身份、单文件上限、分片大小和数量必须同时成立。脚本在 precreate
之前检查精确字节数和 ceil(size/chunk)。具体边界与事故见 references/upload-and-resume.md。
不能只因预创建成功就承诺整次可完成。Cookie/网页与 OpenAPI 的规则不可混用。
没有完整验证的 Cookie 适配器不接大文件，改用已有原生客户端。

## 原生客户端分支

NATIVE_ACTION_REQUIRED 是待操作状态，不是上传成功。读取
references/native-desktop.md，使用真实可用的 Computer Use 入口完成精确上传、
远端观察和独立下载。返回原任务，不替换为删除源文件的历史归档脚本。
原生模式验证要传 --remote-evidence：证据必须绑定任务、远端路径、大小和真实
下载路径，并引用新鲜原始观察的路径及 SHA。不可为了让校验器通过编造观察。

## 续传和收尾

- 保存 uploadid 后才发第一片，每片收到有效响应后立即持久化 ACK。用原
  account/path/uploadid/chunk size/摘要恢复，只补未确认片；改变分片大小是新尝试。
- 默认4并发，允许显式1–8并发。每片最多3次；有连接、读响应、总时限。
  180秒没有新增确认回执则停止派发并保存进度，不把进程存活当成有效进度。
- PAUSED 可恢复；PRECREATE_INTENT/CREATE_INTENT 是控制请求结果未知，先核远端，
  不盲目重放。旧失败、旧分片及原版本保持。
- UPLOADED 只证明合并回执。完成验收还需精确远端路径/大小及独立下载完整 SHA；
  ZIP/7z另做完整性检查。未验证时明确报告尚缺哪一步。
- 上传、下载和验真都不删除源文件。若用户另外要求释放本地副本，转入
  local-disk-maintenance 并沿用当次明确授权，不能自动继承旧论文的删除范围。
- 成批按包恢复大量文件、并行预取、包边界续跑和释放本地副本前的三重核，
  读 references/bulk-restore-and-release.md。

