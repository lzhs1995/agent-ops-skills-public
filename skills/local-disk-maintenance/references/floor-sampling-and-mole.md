# 长任务容量底线与Mole预览

## 底线判断要连续采样

并发进程会造成瞬时容量下跌。实测（2026-11-04，一台Mac）：几十秒内可用量
从约28GB跌到1.9GB，再回到约15GB；另一次是20GB→4.9GB→14.9GB。lsof显示，
原因是别的程序为会话副本临时复制约4.19GB的大文件，多个进程各复制一份。

长时间恢复或下载任务在每个新包及独立下载、装配等新增写入前核可用量，
为本段暂存峰值和既定底线留足空间；包边界取min(statvfs, APFSContainerFree)，
间隔数秒至少读两次，再决定是否开始下一包。低于底线就暂停等待、复测，不要放弃整个任务；外层续跑次数
设上限。临时副本的归属和句柄核对按platforms-and-recovery.md处理：别人的副本
不删，也不计入本方可回收量。不要因为一次瞬时读数低于目标就扩大清理范围。

## 近期写入与测量能力

近期文件和打开句柄是容量归因的线索，需结合同一device/inode在两时点的大小、
分配量及进程状态。近期mtime文件的st_blocks总量是现存分配量，不是期间增长。
活动临时副本仍占真实容量，不能扣除它们的大小来放宽写入底线。

先核实际find实现及参数支持。一台Mac上的bfs拒绝`-newermt '-2H'`，错误在stderr；
接`wc -l`后得到0不能证明没有变化。可用该平台date生成明确截止时间（BSD示例：
`date -v-2H '+%Y-%m-%d %H:%M:%S'`），记录时间与时区，再交支持-newermt的实现。
保留查找命令自身的退出码和stderr，不能只看管道末端的成功状态。

APFS克隆可报告接近全尺寸的st_blocks；与st_size接近不证明没有共享块。
同SHA、同大小或“模板大小+4096字节”也只能提供排查线索，不能单独判定克隆。
已有cp -c探针观察到该现象，但同期statvfs波动不能精确量出克隆的独占成本。
优先复用本技能已验证的private_bytes测量，并单列压缩资源叉、硬链接等边界。
曾有临时ctypes探针对新建非空对照文件也返回0，另一物理块查询返回EFAULT；
这些探针结果不可用，根因未确认，不能据此否定API或认定结构体偏移错误。

无法可靠测独占量时报告UNKNOWN。statvfs/diskutil用于判断当前实际余量，前后差
仍含并发写删、快照、缓存及swap变化，不能直接当本方独占回收。不得为估计收益
自动删除未授权样本，也不扩大既定清单以“压过噪声”。

## 低于目标时的顺序（Mac）

目标值（例如保留40GiB）放在本机profile或当次指令里，不写成技能常量。

1. 用agent-disk check --details读实际容量，并按上节连续采样。
2. 对可备份的冷文件，归档前先核独占量、预计回收收益和暂存峰值：共享块克隆
   删掉几乎不增加可用空间，打包反而可能新增占用。收益和容量满足时再用
   baidu-netdisk-transfer上传、独立下载验真，按当次授权释放本地副本。
   低空间暂存规则见该技能的references/upload-and-resume.md。
3. 用Mole预览系统垃圾，按授权执行，再读两次实际容量验收。

## Mole

Mole通过Homebrew安装，命令为mo或mole（https://github.com/tw93/Mole）。
mo --help列出的子命令有：clean、uninstall、optimize、analyze、status、history、
purge、installer、slim、touchid、completion、update、remove。

- 只读或预演：mo analyze [路径]、mo status、mo history --json、
  mo clean --dry-run、mo purge --dry-run、mo installer --dry-run、
  mo optimize --dry-run、mo uninstall --dry-run、mo slim --dry-run。
- 实际执行clean、purge、installer、optimize、uninstall、slim会删除或改动系统内容，
  只能按当次授权和预览清单执行。预览和真实运行是两个独立范围。
- 本次回收按精确清单、inode去重及实际分配量/独占量记录；前后两次实际容量
  单列报告，不把含并发写入、swap变化的全卷差值算成本方收益，也不直接采用
  Mole显示的大小（见platforms-and-recovery.md中关于Mole size的说明）。

### 预览清单与单位

一次Mole 1.68.0只读预览中，52条路径去重后的逻辑量890.40MB、分配量891.49MB，
预览显示832.10MB。没有执行删除，这些数字既不证明无克隆，也不证明预览是
保守可释放量；差异需先对齐清单、跳过项及计量口径，不能推广固定折扣率。

- 该版clean-list.txt条目含`路径  # 大小`；先核格式，再移除确认的末尾大小注释。
  不对整行lstat，也不按路径中任意`#`截断；保留原清单用于复核。
- 明确显示值是GB还是GiB，换算后再比较门槛。一次Free space读数接近statvfs
  不证明所有版本、卷和时点都等价；正式容量验收仍按上文连续采样。
- 保留白名单和正在使用的工具链依赖，不为凑空间把保护项移出白名单。
- 系统缓存未获权限或被跳过时注明预览不完整，不把未扫描部分计入收益，
  也不因补齐预览而自动提权或执行清理。
- 预览里标`would clean`却没写大小的项（例如pnpm、Go module cache）由工具自己的
  命令清理，不计入预览合计，也不在clean-list.txt里。一次Mole 1.68.0实测：预览合计
  848KB、17项，真实运行删除1.32GB、28项，其中Go module cache占1.30GB。它在
  operations.log里没有对应的REMOVED行，只在运行输出里写了大小。所以预览合计
  不是收益上限，也不能据它判断“清理没有意义”；执行前要看这些项会不会让正在用的
  工具链重新下载，执行后仍按两次实际容量验收。
