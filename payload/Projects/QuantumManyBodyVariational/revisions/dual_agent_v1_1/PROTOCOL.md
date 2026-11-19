# 动态索引与可恢复交接修正版

本版按2026-11-16用户要求修复动态索引支持和交接失败恢复. 旧src, physics_first_v1_1, dual_agent_v1, schema恢复版本及全部历史试次保持原始字节; 新版不是对旧实验成绩的重算或覆盖.

## 索引语义

Python f仍使用原ideal-real值和exact-rational分支语义. 新增晶格坐标及同构tuple的动态整数索引, 包括循环变量, helper参数和整数表达式, 例如cell[d]和cell[1-d]. 支持Python负索引: 长度N的容器要求-N<=i<N, 负数先加N. 不用取模把越界索引伪装成有效索引. 实际执行的越界访问抛受控CorrelatorError; Lean生成同样的索引选择和必要的路径敏感域义务, 未执行分支不增加无关前提. 异构tuple动态结果不能归为一个支持类型时明确拒绝, 不猜测类型.

无限态的固定目标仍为StateCondition D Q f和NumericalPreconditions, 公理集合, 维度, 每晶胞qubit数及能量归一化不改变. 新proof lock绑定新版compiler/verifier/callback, 继承原基础规格与Lean/mathlib工具链.

## 交接失败恢复

每次交接尝试使用持久的唯一编号, 保存源码和数学证明的只读快照及preparing/failed/committed状态. owner.handoffs和当前handoff仅包含完成提交的记录, 失败尝试通过handoff_attempts单列. 提交过程用文件锁保护编号分配和提交; 新broker重建时扫描并识别旧提交及残留目录, 不从内存成功记录长度复用已经占用的编号.

明确表示限制可按旧约定带technical_diagnostic交给formalizer, 不成为认证. 意外宿主异常保存安全的异常类型与栈位置, 返回可恢复的host_exception; 失败不会替换上一份完整提交或产生认证能量, 下一次修改或重试仍可交接. 中途写入失败或进程中断留下的记录不冒充committed, 不覆盖或删除过去证据.

## 运行和验证范围

默认开发入口由config/active_agent_revision.json与scripts/dual_agent.py选择本版本. 新run_trial/run_campaign使用新Tools, callbacks, compiler, verifier及独立锁, 并沿用已经完成的API-source兼容客户端. 新analyze使用新锁, 旧分析入口继续保留旧含义.

统计器以有效committed标记及其manifest哈希绑定识别成功交接, 失败和中断尝试单列审计, 不让残留manifest改变当前有效候选. 已提交记录损坏时明确标注证据问题, 不把缺失资料冒充已核验结果.

构造agent与形式化agent的职责及预算沿原dual-agent协议. 此次实现和本地验证不启动付费模型, 不自动续跑或补测二维TFIM. 原schema恢复runner只服务其原来冻结版本, 不被冒充为新版任意交接故障的通用恢复器. 本版的交接恢复指失败后可在同一broker或重建broker中继续提交, 不清零或重建LLM会话预算.

回归覆盖实际导致故障的源码, 动态正负索引, 越界拒绝, Lean域义务, 失败注入, 孤儿目录, broker重建, 旧有效交接保留, 源码冻结和严格核验后才返回能量. 大表达式及单次Lean计算仍受原资源上限约束, 不把受控资源拒绝写成数学反例.
