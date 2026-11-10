---
name: local-disk-maintenance
description: 在 Mac 或 Windows 快速定位磁盘占用，复用既有盘点和回执，规划与执行当前已授权的有限缓存清理，Mac可对关闭冷文件做保全内容的APFS压缩，并按实际容量验收。
---

# 本地磁盘快速治理

入口为本skill的scripts/disk.py；安装命令agent-disk。运行层读取本机disk profile，
其中cache_roots是已确认的可再生缓存，protected_paths是删除保留范围，
compression_exclusions是压缩排除范围。用户当次指令和既有授权优先。

## 高效顺序

1. agent-disk check --details，先看实际可用量、Mac的MEM/CMPRS及swap，
   或Windows的原生容量与pagefile。低RSS不能排除几十GiB换页占用。
2. 复用已有目录盘点，先读原SUMMARY/EXIT，再定向核回执。限制搜索深度和条数；
   HANDOFF可能滞后，原终态与进程实况共同决定剩余范围。
3. 对当次已授权范围生成固定plan，展示候选、排除、是否扫描完整及目标缺口。
   既有授权覆盖该具体范围时直接执行，不重复询问。没有授权的模型、历史分卷、
   会话、研究原件和正在使用的运行时不进入缓存删除。
4. apply只执行这份清单：身份/完整SHA变化、打开句柄、原尝试存在就保留或停止。
   不因想凑到40/100GiB而扩大范围，不启动其他软件制造新的可清理对象。
5. verify核原回执和两次实际容量。全卷变化包含并发写入与swap，不等于本方收益。

~~~text
agent-disk check --details --target-gib 100
agent-disk plan --kind cache --target-gib 100
agent-disk plan --kind apfs --root 当次授权的关闭冷文件目录
agent-disk apply --plan 返回的PLAN.json --authorization 当次已获得的具体授权
agent-disk verify --summary 返回的SUMMARY.json
~~~

check不执行清理。plan默认最多检查10000个目录项、7天冷却；达到上限明确
scan_partial，不冒称全盘已扫描。没有指定目标时只报告处理范围和容量。
缓存只来自本机配置允许的根目录，不因为文件夹名字含cache就允许删除。

Telegram 清理先读 [references/telegram-cache.md](references/telegram-cache.md)：
优先应用内存储管理；完成缓存可能与 partial 共用 inode，普通 cache 计划会保留
这些多硬链接文件。保留自动下载的选择不变，不能按路径大小重复计收益。

## 平台与压缩

读references/platforms-and-recovery.md。APFS数据叉独占量不等于du；
Mac关闭冷文件用原生临时克隆、两条压缩线程、逐文件完整SHA/属性验证。
只读回成功才释放本次临时克隆。前两个文件无文件级收益即收尾。
原生验收还须有压缩标志和分配量降低；父进程不可持有压缩器所需的源文件锁。
Windows不执行APFS压缩；删除缓存前以原生独占句柄验证精确文件。

若要使用Mole，先读已安装mole skill及对应平台reference。Mole的预览与真实
运行是独立范围，不将其跳过/保护项交给本工具绕过删除。预览量及st_blocks与
st_size接近都不证明独占量；缓存和归档均不能套用固定回收比例。
长任务容量采样与 Mole 预览见 [references/floor-sampling-and-mole.md](references/floor-sampling-and-mole.md)。
归档上传后释放本地目录的收益口径与暂存峰值见 [references/archive-release-capacity.md](references/archive-release-capacity.md)；
同文件还有克隆共享的物理块探针、整树回收的下限与估计、全目录独占量盘点和释放任务的运行底线。

已有执行目录不能重开；先查其SUMMARY、STOP、逐文件RESULT和真实进程。
中断恢复单独核收原尝试，不为了整齐改掉失败记录。完成后报告真实余量、
达标与否、保留范围、剩余状态和原回执路径。技能安装不启用自动后台清理。

