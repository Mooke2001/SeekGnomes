# 无限多qubit晶胞态的固定基础规格v2

v2保留v1的全部文件和SHA256锁. 本版站点为`Site d q := (Fin d → ℤ) × Fin q`, 允许一个无限晶格site/晶胞具有有限多个qubit. `q>0`由评测方局域能量对象保证. 每个内部自由度仍是二维qubit, 不把整个晶胞当成一个qubit.

态仍是唯一全局关联函数`f : Word (Site d q) → ℝ`. `StateCondition d q f`要求归一化和任意有限区域上的完整Pauli重建矩阵正半定, 同时要求对任意整数晶胞位移平移不变. 平移仅移动坐标, 不改变内部qubit编号. 不要求胞内qubit置换对称性, 也不预设MPS/PEPS或有限系统序列.

局域Hamiltonian使用评测方拥有的`LocalEnergy d q n m`. 全部有限Pauli项共享同一个注入`placement`. 实际矩阵为`matrix`, 每晶胞期望为`cellValue`, 每qubit评分为`value := cellValue / q`. 常数项用空Pauli词表示并保留, 不额外除以配位数. `cellValue_eq_trace`, `matrix_isHermitian`和`value_eq_trace_re`把分数与真实复Pauli矩阵联系. 认证单项误差可通过`value_error_bound`和`value_le_upper`传播.

## 固定提交接口

提交单一Lean文件, 定义`Submission.f`, `Submission.bound`和`Submission.accepted`. 允许任意能够在Lean中证明合法的无限态及辅助证明, 不限制态构造白名单. `Submission.accepted`的精确类型必须为:

```lean
CandidateAccepted TrustedTask.task Submission.bound Submission.f
```

其中`TrustedTask.task`由可信进程从完整任务JSON生成, 不使用候选提供的任务定义或任务摘要作为替代. `Submission.bound`必须定义为本次评分调用给出的确切有理数字面量. comparator只开放`Submission.f`的定义体, 固定bound以及任务, 归一约定和合法性定义. 因此分数是核验过的合法态能量上界, 不是候选自报的数值. 若只证明宽松上界, 该宽松上界就是成绩, 不自动视为精确能量.

JSON任务字段为`dimension`, `qubits_per_cell`, `energy_normalization:"per_qubit"`, `terms:[{coefficient:"p/q",word:[{cell:[...],qubit:0,pauli:"X"}]}]`. Pauli词中同一站点不得出现两次. `word:[]`是恒等项. `id`, 名称和参数等元数据保留用于溯源, 真实语义仍由完整Pauli和决定.

## 防改题与运行隔离

入口为`Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/check_state_spec_v2.py --task-json TASK.json --upper UPPER --candidate-source Candidate.lean`. 也支持`--solution-export Candidate.ndjson`, 此模式不运行候选源码.

源码模式将候选置于无网络的bubblewrap命名空间. 仅挂载Lean工具链, 已锁定库的编译依赖和本次只读任务, 候选只能写自己的工作目录和临时目录. 主机凭据, 其他模型的记录和项目其他文件不挂载. 项目编译库另建只含通用语义模块的快照, 不挂载v1已解任务或v2回归任务, 防止绕过源码import检查后枚举历史任务答案. 通用helper仅提供tracial, plus和凸混合的合法性工具. 允许导入可信v2接口, host生成的`TrustedTask`, 以及已锁版本Mathlib模块. 候选不能写可信任务或challenge.

编译器输出仍按不可信数据处理. 候选目录中的符号链接, 硬链接和特殊文件会被拒绝, 提交源文件必须与编译前冻结的原字节一致. 主机读文件逐层禁止符号链接, 并检查文件类型与链接数; 创建输出使用`O_EXCL|O_NOFOLLOW`, 不截断任何既有路径. 经过检查的olean及其伴随文件被复制为只读快照, 导出进程只能写全新scratch. 导出stdout通过主机预先独占创建的文件描述符接收, 再安全复制到可信`judge-input.ndjson`; comparator只读取该冻结副本. 这防止编译期代码或import initializer制造路径别名, 诱导后续主机I/O越界读取或覆盖文件.

独立comparator比较完整命题和全部非孔依赖定义, 检查公理, 并调用Lean默认kernel重放. 仅允许`propext`, `Quot.sound`, `Classical.choice`, 因而候选证明依赖的`sorryAx`和任何候选新增公理均被拒绝. 为匹配comparator要求的theorem声明种类, 生成的challenge使用`by sorry`占位, 仅描述固定目标形状. 它不属于语义核心, 不声称每个提交上界均可达到, 不作为候选证明的公理来源. candidate的真实证明仍须独立通过公理检查和kernel重放. 额外公理与`sorry`候选的拒绝回归检验这条边界. 评测器不把候选打印的公理清单或回执当成核验证据.

`STATE_SPEC_V2.lock.json`绑定v1锁, v1全部源依赖, v2语义, 检查器和工具链. 每次运行记录任务完整JSON, 生成的Lean任务, 候选源/导出哈希, 规范锁哈希, comparator日志与前后锁核对. 失败不会返回有效证书. 哈希保证版本一致性, 不证明自然语言与数学语义一致.

本地Lean核验与211数值工具的额度分开登记. 所有并发checker共享宿主允许CPU集合中的固定前2个CPU, 通过操作系统亲和性限制, 并用继承式seccomp禁止扩大该集合. 候选命名空间允许Lean工作线程, 禁止fork/vfork和非线程clone, 以避免编译期代码开启额外进程进行未登记的大规模搜索. 进程地址空间上限12GiB, Lean自身内存参数8GiB. 核验调用数及墙钟上限由统一agent预算控制. `-j2`本身不被当作操作系统资源隔离.

## 范围和限制

任意可证明的态可提交, 但本版没有声称对任意非计算性`f`自动求值. 评分接受候选提供并证明的有理上界, 也可通过有限查询认证其近似误差. 本版没有形式化普遍热力学变分定理, GNS, 准局域C*范数完备化或搜索收敛性. 不把一般二维/三维TFIM标成精确可解. 各具体50模型的晶格键表与能量分摊还须分别审定, 通用Lean接口本身不能取代这份物理语义责任.
