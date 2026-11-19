# SeekGnomes

**简体中文** | [English](README.md)

SeekGnomes让LLM为无限量子晶格构造低能量的合法态, 并由固定Lean目标及独立内核核验. 源码入口默认使用`seekgnomes-harbor-state/v1`协议. 它以Harbor0.22.0组织任务和试次, 使用模型公司的官方harness. 公司没有官方harness时使用Codex. v1.3.0包含`.state`工作流, 试次检查点恢复和本地浏览器控制台.

## 快速开始

下载[**完整构建包**](https://github.com/Moke2001/SeekGnomes/releases/download/v1.3.0/seekgnomes-v1.3.0-public-build-context.tar.gz)并解压. 初次构建需要Linux x86-64或x86-64 WSL, Python 3.11+, Docker Engine或Linux rootless Podman, 以及访问固定依赖上游的网络. 容器运行需要至少12 GiB当前可用内存, 建议16 GiB, 无GPU要求. 镜像构建步骤见[BUILD.md](BUILD.md).

按构建说明完成镜像后, 在同一目录执行:

```bash
./seekgnomes doctor
./seekgnomes self-test
bash ./console-src/Start.command
```

启动器打开本机网页. 选择模型、API、5至50题、CPU、并行数、token或时间预算、联网和可选结果共享. 命令行可直接运行`./seekgnomes`. Windows使用WSL与Linux容器; 其他平台的实测范围见验证摘要.

交互入口会询问精确model ID, API地址和隐藏API key. 公司识别不明确时, 用`--company`明确指定. 也可先查看选择结果, 不发模型请求:

```bash
./seekgnomes harnesses
./seekgnomes run --company openai --model YOUR_EXACT_MODEL_ID \
  --api-base-url https://YOUR_PROVIDER.example/v1 \
  --api-protocol responses --tasks 2 --print-config
```

实际运行去掉`--print-config`. `--tasks all`为默认50题, 可用`--tasks 1,7,13-15`选择明确子集. 默认允许通过受控工具查公开资料, `--no-web`记录为不同协议. API key从隐藏输入或`SEEKGNOMES_API_KEY`环境读取, 不使用命令行key参数.

`--constructor-output-tokens`设置Constructor主阶段的累计输出token限额, 默认128000, 也可通过`SEEKGNOMES_CONSTRUCTOR_OUTPUT_TOKENS`配置. 本版取消1x/2x/4x的科学扩容. 主阶段结束时, 只有从未成功通过`create_handoff`交接完整`f.state`的试次, 才追加唯一一次固定50000输出token的收尾, 要求完成定义并交接. 收尾沿用该试次会话, 结束时仍无完整交接则为科学失败.

`--cpus`同时控制容器CPU配额和运行计划, 默认2. `--constructor-active-seconds`配置主阶段活动时间, 默认14400秒; 符合条件的收尾另给同样的时间额度, 因而每个技术attempt内Constructor总活动时间最多为两倍该值, Formalist活动期间暂停Constructor计时. 输入token默认没有累计上限, 可选`--constructor-input-tokens`显式设置主阶段额度I, 符合条件的收尾独立再给I; API次数不设累计上限. 对应环境变量为`SEEKGNOMES_CPUS`, `SEEKGNOMES_CONSTRUCTOR_ACTIVE_SECONDS`和`SEEKGNOMES_CONSTRUCTOR_INPUT_TOKENS`.

## 两个角色

Constructor负责物理探索, 编写完整的`f.state`, 以`create_handoff`交接. 宿主冻结该文件并从它生成Lean定义与固定命题. Formalist通过`prepare`收到生成文件, 编写证明并以`score`提交证明文件. 证明经固定命题匹配与独立Lean内核验证后, 宿主用同一份`.state`计算每qubit能量密度, 将结果返回Constructor. 未核验通过时Formalist继续修订证明. 发现冻结定义的具体缺陷时可以反馈给Constructor修正. 两个角色可从`reference/state_language.md`读取同一份[.state使用文档](STATE_USAGE.md).

原生harness管理各自上下文, 旧的按字节阈值裁剪消息循环不再作为本版执行器. 框架内部的摘要或压缩请求仍计入相应角色的用量. Constructor预算贯穿每次交接, Formalist按既定协议不限制累计用量; 单次API和Lean进程仍有期限与资源保护.

本版取消旧的3600秒累计CPU上限, 按所选CPU配额和活动时间控制资源. 数值工具记录请求CPU数乘以请求wall时间的保守预留, 原生CLI记录进程树观测CPU, 二者分别保留, 与旧版用量不直接并表.

## 结果

宿主结果默认位于解压目录的`results/`, 可通过`SEEKGNOMES_RESULTS_DIR`指定. 容器内固定映射为:

```text
/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs
```

不要把宿主路径传给`--out`; 需要指定时使用上述容器内目录下的新路径. 每次运行保留计划, Harbor结果, 两角色轨迹, 定义原稿, 候选代码, 证明和核验回执, 并输出`results/`和报告. `report`只读取已有证据, 不发API请求.

```bash
./seekgnomes report \
  /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs/YOUR_RUN
```

只有固定命题匹配且通过独立Lean内核的态才计入成绩. 报告使用原始`.state`与核验记录重算能量. 能量为浮点估计, Lean核验覆盖态的合法性, 平移不变性及相关定义域和极限条件. 技术缺失不填0, 严格可解组和一般变分组分别评分, 详见`SCORING.md`及`PROTOCOL.md`.

预算, CPU或联网设置不同的运行单独记录配置, 不直接混作同一排名. Harbor在试次控制器意外退出后自动从同一试次的检查点接续. 试次进入需要独立重测的技术终态时, 每题最多3个同配置的技术attempt. 科学失败保留原预算内的结果.

如外层监督进程或宿主也已退出, 用包含检查点续跑支持的构建重启原运行:

```bash
./seekgnomes resume \
  /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs/YOUR_RUN
```

容器启动时应挂载同一个结果目录, 并用`SEEKGNOMES_CPUS`提供原计划所需的CPU数. 续跑从原计划读入模型和预算, API key仍通过隐藏输入或环境提供. 已完成任务与明确取消的运行保留原终态.

## 安全和复现边界

外层包仅挂载本次results目录, 不挂Docker socket或宿主用户家目录. 原生agent在受限角色环境中运行, 只获得角色范围的工具连接, 不获得真实模型API凭据. Constructor的科学计算工具和Formalist的证明工具分别隔离. 禁止跨模型, 跨任务或跨独立运行/技术attempt共享候选和长期记忆.

`doctor`和`self-test`不发付费模型请求. 自检包含严格核验正反例, `.state`从证明到能量的本地验证, 以及离线Harbor试次. 原生CLI和平台的验收范围见对应包的`validation-summary.json`.

## 发布文件

- `seekgnomes-v1.3.0-github-source.tar.gz`是轻量源码. 含题库、Lean规格、执行器、本地控制台和构建配方.
- `seekgnomes-v1.3.0-public-build-context.tar.gz`另含固定Lean、Mathlib和Python wheel. 需在本机按构建说明获取厂商CLI并构建镜像.

公开附件提供[SHA-256清单](https://github.com/Moke2001/SeekGnomes/releases/download/v1.3.0/seekgnomes-v1.3.0-SHA256SUMS). 项目自身的许可条款尚待作者指定, 第三方组件适用各自的许可. 包内无模型权重、API key、历史测评数据或公共评测服务器. 结果共享默认关闭, 需在发布方配置接收地址后主动启用.
