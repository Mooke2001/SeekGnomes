# 无限平移不变qubit态的固定基础规格v1

本规格落实用户要求: "把这份语义责任集中到一套小而固定的基础规格上". 唯一被搜索的态对象为全局关联函数\(f\). 直接研究无限态, 强制平移不变, 不要求候选构造有限系统序列. 当前正式任务取\(\mathbb Z^2\)和\(\mathbb Z^3\), 每格点一个qubit, 允许纯态或混态.

此文档是需要集中审阅的语义合同. Lean核验其形式命题的证明, comparator检查候选没有替换固定命题或相关定义. 它们都不自动证明自然语言合同与形式定义的对应关系. v1仍经独立LLM源码审核, 未冒称人类专家验收.

## 固定的对象和含义

| 对象 | 形式定义 | 固定的物理含义 |
|---|---|---|
| 晶格 | `Lattice d := Fin d → ℤ` | 任务固定`d=2`或`3`, 无限方格或简单立方格 |
| Pauli串 | `Word Γ := Γ →₀ Fin 4` | 0, 1, 2, 3依次为I, X, Y, Z, 仅非I位置具有有限支撑 |
| 态对象 | `f : Word Γ → ℝ` | 标准Hermitian Pauli串的期望值, 输入不携带独立整体相位 |
| 局域矩阵 | `localDensity f e` | 实际复Pauli矩阵的完整重建, 系数为\(2^{-n}\) |
| 合法性 | `IsLegal f` | \(f(I)=1\), 且任意有限区域和任意注入放置的重建矩阵均PSD |
| 平移不变性 | `IsTranslationInvariant f` | 对任意格点平移\(a\)及任意串\(P\), \(f(\tau_aP)=f(P)\) |
| 候选态条件 | `StateCondition d f` | 合法性与平移不变性的合取 |

Pauli矩阵采用\(X=\begin{pmatrix}0&1\\1&0\end{pmatrix}\), \(Y=\begin{pmatrix}0&-i\\i&0\end{pmatrix}\), \(Z=\operatorname{diag}(1,-1)\), 因而本征值为\(\pm1\), 不使用自旋\(S^\alpha=\sigma^\alpha/2\)的另一归一约定.

有限区域只用于描述同一个无限态的局域观测和正性. 任意有限区域上的量词不是有限次数值抽检, 也不是有限体积态序列. 它不限制MPS/PEPS形式, 支撑大小或关联距离.

全部Pauli项必须放在同一个真实坐标系中. 可以将整个查询串整体平移以利用TI, 不能把参与乘法的各个因子独立搬到原点而丢失相对位置.

## 固定的能量和接收命题

`LocalEnergy d n m`由评测方提供, 含共同注入`placement`, \(m\)个有理系数及\(m\)个局域Pauli串. \(n,m\)描述局域能量项的有限支撑与展开长度, 不限制候选无限态的表达空间.

\[h_0=\sum_{j=1}^{m}c_jP_j,\qquad \operatorname{value}(h_0,f)=\sum_{j=1}^{m}c_jf(P_j).\]

已证明`matrix_isHermitian`及`value_eq_trace`:

\[h_0^\dagger=h_0,\qquad \operatorname{Tr}(\rho_e(f)h_0)=\operatorname{value}(h_0,f).\]

因此评分函数与实际Pauli矩阵期望相连. 该等式本身不需要正性; 接收候选时另外要求全局态条件. 通用模块能表达任意有理系数局域Hermitian项, 但只有经过评测方审定的每格点能量分摊才被解释为指定哈密顿量的能量密度.

当前两项物理任务固定为:

\[h_0^{(2D)}=-gX_0-JZ_0Z_{(1,0)}-JZ_0Z_{(0,1)}.\]

\[h_0^{(3D)}=-gX_0-JZ_0Z_{(1,0,0)}-JZ_0Z_{(0,1,0)}-JZ_0Z_{(0,0,1)}.\]

每条正坐标方向无向键计一次, 场项每格点计一次, 单位为每qubit能量. 不额外除以配位数, 不重复计入反方向键. 正式防改题回归固定二维任务\(J=g=1\); 三维任务在Lean库中另有相同接收条件的成功实例.

候选必须证明固定命题:

```lean
CandidateAccepted task bound f :=
  StateCondition d f ∧ task.value f ≤ (bound : ℝ)
```

`task`归评测方所有. `f`是唯一态对象. `bound : ℚ`是候选声称并证明的能量上界, 不是额外态参数. 允许候选增加任意可核验的辅助定义和证明, 不设置态构造白名单.

`value_error_bound`及`value_le_upper`还允许从有限个认证有理查询及各自误差界推出候选真实能量上界. 它们引用同一个`f`; 不能只提交数值而不证明数值与`f`对应. 包围的是候选态能量, 不能把整个区间冒称基态能量区间.

## 候选不能修改什么

固定参考模块`SpecChecks.Challenge`使用最大混合态和上界0给出一个真实已证实例, 无未完成证明. 与它比较时, 只开放两个定义体:

```text
Submission.f
Submission.bound
```

两者的类型和safety也固定. `Submission.accepted`的完整类型固定为`CandidateAccepted (tfim2D 1 1) Submission.bound Submission.f`. 其他相关定义, 包括真实Pauli矩阵, 正性, 平移, `LocalEnergy.value`, TFIM参数和计数均不属于定义孔.

`lake comparator`直接比较导出的声明, 递归检查相关非孔常量的完整信息, 检查证明依赖的公理, 并在独立进程中进行Lean内核重放. 裁决进程不导入候选的`.olean`. 允许公理仅为`propext`, `Quot.sound`, `Classical.choice`. 这比依赖候选自行打印的公理清单更强, 但仍以工具链与固定参考正确为信任前提.

`STATE_SPEC_V1.lock.json`绑定语义基础, 依赖源码, 正式配置, 检查器和文档的SHA256以及Lean工具和mathlib版本. 检查入口只核对锁, 不提供自动刷新锁的选项. 修改被锁定文件会阻止验证; 有意修订规格应经重新审阅并建立新版本. 哈希绑定版本, 不证明语义正确.

## 运行与回归

从Eureka根目录运行:

```bash
Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/check_state_spec.py
```

默认构建已审阅的库和测试模块, 核验35项核心定理的公理依赖, 然后对以下导出对象逐一裁决. 所有反例均单独通过Lean编译, 预期由comparator拒绝, 所以能检验"编译通过仍然曲解目标"这一风险.

| 回归输入 | 预期 |
|---|---|
| 参考最大混合态, 上界0 | 接受 |
| 改为全\(\lvert+\rangle\)态, 上界-1 | 接受 |
| 将接收目标换成`True` | 拒绝 |
| 将固定场强\(g=1\)换成2 | 拒绝 |
| 为目标增加前提 | 拒绝 |
| 依赖额外的`False`公理 | 拒绝 |
| 沿用相同完整任务名和类型, 却把任务函数体替换为零能量 | 拒绝 |

故意含额外公理的文件只属于拒绝测试, 不由正式库入口导入, 也不在允许的公理列表中. 同名声明测试模块相互独立, 不能同时导入一个环境.

第三方候选可先在隔离构建环境导出NDJSON, 然后提交给同一入口:

```bash
Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/check_state_spec.py \
  --solution-export /absolute/path/to/candidate.ndjson
```

此入口复制并绑定候选导出字节, 可信参考始终从本地已锁源码生成, 不接受候选提供的challenge或comparator配置. 裁决使用两份导出, 跳过候选项目的依赖解析和源码执行, 并在外层无网络namespace及只读文件系统中运行; 不使用`--inadvisably-no-sandbox`. 开发构建只面向当前锁定的课题源码, 不把任意第三方Lean源码当作可信输入直接编译.

每次结果写入`tmp/state-spec-check-<UTC>/manifest.json`, 包括规格锁哈希, 构建和公理日志, 导出字节哈希, 裁决退出码和前后锁检查. 第三方模式证明导出的候选满足固定命题, 尚不自动提取或执行任意非计算性`f`及`bound`表达式, 也没有接入LLM排行榜.

## 本版未声称完成的内容

本版完成局域能量表达与实际矩阵期望的桥接及认证误差传播. 标准热力学能量密度的普遍变分定理\(e_0\leq e(f)\), 准局域C*完备化/GNS, 任意\(f\)的可执行任意精度求值器, 以及基态性和搜索收敛证明尚未形式化. 文档中的每格点能量解释由固定任务的物理计数约定和集中源码审核支持, 不以编译通过替代这份语义责任.
