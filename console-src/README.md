# SeekGnomes本地测评控制台

按发布包的`BUILD.md`准备依赖并在本机运行`./build-image.sh`. 使用构建镜像的同一容器引擎, 在普通用户终端运行以下命令.

```bash
bash ./console-src/Start.command
```

若使用本地组装的`seekgnomes-console-v1.3.0-linux-amd64.tar`, 解压后运行`./Start.command`. 启动器打开本机浏览器中的SeekGnomes控制台. 如果系统没有自动打开浏览器, 使用终端打印的本机网址. Windows可在已配置WSL的情况下双击控制台目录的`Start WSL.cmd`, 或在WSL终端运行对应命令. 保持启动终端开启, 浏览器标签可以关闭并重新打开. 关闭控制台时, 正在运行的容器按停止流程处理.

需要Python 3.10或以上, Linux amd64容器, rootful Docker或rootless Podman. 容器默认分配16 GiB内存, 首次导入镜像建议为引擎存储预留至少40 GiB可用空间. 已有定制Docker套接字时继承终端的 `DOCKER_HOST`. WSL的包如果解压在 `/mnt/d` 等Windows挂载, 结果默认落到WSL原生文件系统, 以便Unix socket和文件权限可用. 可通过 `SEEKGNOMES_RESULTS_DIR` 指定其他支持POSIX权限和Unix socket的位置.

在页面填写模型公司, 精确模型ID, API地址和API key. API选项来自当前运行版的harness注册表. Kimi Code需要显式窗口, 请填写服务提供的窗口值. DeepSeek DSH使用Messages协议. 选择题库中的5至50道题, 设置CPU, 题目并行数, 预算和联网权限, 点击 **Start benchmark**. 控制台核对容器中的源码清单. 本机已构建的匹配镜像可以直接使用.

题目并行数默认为1, 上限是所选题数与总CPU数中的较小者. 设为2时, 两道题可同时运行, 每道题独立执行Constructor与Formalist的交接和核验. 例如总CPU设为4, 并行数设为2, 每道题分配2个CPU. 总CPU无法整除时向下取整. 页面列出当前运行的题号, 一道题结束后下一道排队题目可启动. 点击 **Stop** 停止整批, 保留已写出的记录.

预算针对每题的Constructor主阶段. Token模式累计服务商报告的输出token, 包含服务商归入其中的推理用量. 时间模式按Constructor活动时间计, 等待Formalist时暂停. 主阶段未交接定义时, 仍按既定协议给唯一一次最多50000输出token的收尾. 时间模式为收尾另给与主阶段相同的活动时间. Formalist维持原协议的累计不限额. 单请求的输出和超时保护依然生效, 两种模式都不是费用上限.

运行过程中页面显示任务进度. Constructor提交`f.state`, Formalist证明固定目标, 宿主通过Lean核验后计算能量密度. Harbor监督进程恢复意外退出的试次控制器. 外层进程退出后, 单路运行可用CLI的`resume`恢复同一批次, 并行运行可逐一恢复已落盘的分片检查点. 点击**Stop**属于主动取消. 完成后可下载JSON分数, 完整核验材料在本机结果目录. 不同题集, 预算模式和联网配置适合分开对照.

共享默认关闭. 发布方配置了接收地址后, 勾选共享的运行会在结束时自动上传结果, 页面显示回执. 接收池与重放核验流程见 [SHARING.md](SHARING.md).
