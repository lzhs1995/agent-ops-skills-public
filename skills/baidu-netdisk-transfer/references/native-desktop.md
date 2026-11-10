# 已登录原生客户端

Mac真实可复用入口是com.baidu.netdisk。使用当前环境的Computer Use获取新鲜
应用状态；确认账号、目标文件夹和传输列表。CDP缺席不需要先修CDP才能继续。
Windows先观察已登录百度客户端。跨机脚本、文件分发和验证使用当前环境已授权的远程工具；
这些外部工具不包含在本套件中，先确认其可用性，不为同一任务新建重复通道。

操作保持原应用的队列与焦点锁；不要沿用旧坐标、旧PID或历史watch消费者。
Mac文件选择器可用Cmd+Shift+G输入绝对目录。剪贴板/输入超时后先重新观察是否
已提交，避免重复上传。不要把别的任务等待状态当作本任务权限。

本机客户端SQLite仅用mode=ro读取；Mac数据库根在本机profile的client_root。
常见表来自filecache.db、transmission.db或upload.db。Windows位置通过本机安装
及进程配置发现，不能把Mac路径平移。只读任务历史用于辅助核对，远端现状仍需
新鲜UI或服务端元数据观察。

验证步骤：

1. 使用plan中的local_path、remote_path和完整SHA，上传一个精确目标。
2. 观察远端目录中精确文件、大小、上传完成状态；保存原始观察。
3. 从远端独立下载到新的本机路径，观察完成。下载文件不能是源文件或其硬链接。
4. 创建本机remote-evidence.json，字段为task_id、remote_path、size、
   downloaded_path、observed_at及observation_paths。后者是
   [{"path":"原始观察绝对路径","sha256":"实际摘要"}]。
   字段必须来自步骤2–3的真实观察，不能把待验证内容手工写成PASS。
5. verify加--remote-evidence，完成SHA和归档测试。保留源与下载副本。

历史baidu_desktop_archive.py带有“上传后删除本地原件”的finalize流程，不能直接
当通用上传器执行。当前skill完全不包含该归档删除入口。

