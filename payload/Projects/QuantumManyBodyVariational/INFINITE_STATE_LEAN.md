# 无限格点qubit态合法性的Lean原型

版本说明: 本文记录首批合法性原型的交付范围. 后续已加入固定能量接口和防改题检查, 当前合同见[固定基础规格v1](STATE_SPEC_V1.md). 下文关于未形式化能量的说明指首批版本, 不覆盖后续增量.

本轮将此前讨论的"一个全局Pauli关联函数是否描述合法无限态"落实为Lean定理. 对象是任意格点类型上的qubit系统, 平移不变性另由格点加群结构定义. 二维方格和三维简单立方格分别取`Fin 2 → ℤ`和`Fin 3 → ℤ`. 所有区域定理都对任意有限类型及其到晶格的注入量化, 没有预设区域大小, 几何形状或MPS/PEPS形式.

本原型采用兼容的有限区域密度矩阵描述无限态. 尚未在Lean中构造准局域C*代数的范数完备化, 也未构造GNS表示. 本轮不包含哈密顿量, 能量密度, 基态性或变分搜索收敛性的形式化.

## 对象与合法性

`Pauli := Fin 4`的四个值依次表示I, X, Y, Z. `Word Γ := Γ →₀ Pauli`表示有限支撑的Pauli串, 默认值0是I. `Correlator Γ := Word Γ → ℝ`是定义在全部有限Pauli串上的一个实值函数. 这一语义空间本身没有有限参数限制. Lean文件给出的有限表述及证明只是其中能够被具体定义和认证的候选.

给定任意有限类型`ι`与注入`e : ι ↪ Γ`, `place e p`把局域串`p`放到无限晶格中. `pauli`是真实的二维复矩阵, `pauliTensor`的矩阵元是各位置Pauli矩阵元的乘积. 局域矩阵严格按以下公式重建:

\[\rho_e(f)=2^{-|\iota|}\sum_{p:\iota\to\{I,X,Y,Z\}} f(\operatorname{place}(e,p))\bigotimes_{x\in\iota}\sigma_{p(x)}.\]

核心定义为:

```lean
def IsLegal (f : Correlator Γ) : Prop :=
  f 0 = 1 ∧ ∀ (ι : Type) [Fintype ι] [DecidableEq ι] (e : ι ↪ Γ),
    (localDensity f e).PosSemidef
```

这里的`0`是恒等Pauli串. `Matrix.PosSemidef`采用mathlib的真实复矩阵正半定定义, 包含Hermitian性质和对所有复向量的非负二次型条件. 将正性写成一个待证命题, 只是规定候选必须满足什么; 后面的具体例子, 闭包和极限定理才是已完成的合法性证明.

对于加群格点, `translate a p`将整个有限支撑平移`a`. 平移不变性的定义是对任意`a`和任意`p`, 有`f (translate a p) = f p`. `IsTranslationInvariantState f`是合法性与平移不变性的合取.

## 已证明的内容

以下结果均由Lean检查, 不是有限尺寸数值测试.

| 内容 | 已证明的精确范围 | 主要定理 |
|---|---|---|
| 归一化 | 任意有限区域的迹等于\(f(I)\), 合法时等于1 | `localDensity_trace`, `IsLegal.trace_one` |
| 关联函数恢复 | 任意局域Pauli串的期望严格等于输入的\(f(P)\) | `localDensity_expectation` |
| 边缘一致性 | 任意有限区域分为保留部分和舍弃部分, 对后者取真实矩阵部分迹, 等于在前者直接重建 | `localDensity_partialTrace` |
| 因子分解证书 | 若统一证明任意区域都有\(\rho_e=V_e^\dagger V_e\), 则\(f(I)=1\)保证合法 | `isLegal_of_factorization` |
| 局域极限 | 逐Pauli串收敛, 最终归一化及各区域最终PSD推出极限合法 | `isLegal_of_tendsto` |
| 平移不变极限 | 在上一行条件之外, 每个平移等式最终成立, 则极限合法且平移不变 | `isTranslationInvariantState_of_tendsto` |
| 凸组合 | 对\(0\leq t\leq1\), 两个合法平移不变态的\(tf+(1-t)g\)仍合法且平移不变 | `IsTranslationInvariantState.mix` |

归一化, 关联函数恢复和部分迹等式不依赖正性假设. 它们是重建公式本身的代数性质. 部分迹定理通过任意有限类型`α ⊕ β`描述区域的任意二分, 没有增加一个未经证明的"兼容性"字段.

局域极限定理允许每个区域有自己的起始指标: 对固定区域`e`, 存在\(N_e\), 使该区域的重建矩阵在\(n\geq N_e\)时PSD即可. 它不要求所有区域同时在某个有限指标后满足条件, 也不要求每个近似\(f_n\)本身已经是无限态. 但是必须证明对每个Pauli串的真实收敛; 只有数值序列或几个有限尺寸检查不够.

任意输入有限态序列\(\sigma_n\)到全局近似关联函数\(f_n\)的通用提取器, 相应边界条件及收敛证明, 目前尚未实现. 因而不能把这条条件定理表述成"任意有限态序列自动产生指定无限态".

## 已通过的例子与反例

`tracial`是无限格点上最大混合单qubit态的乘积, 其\(f(P)\)仅在\(P=I\)时为1. 已对任意有限区域证明\(\rho_e=I/2^{|\iota|}\), 并证明其全尺寸合法性和平移不变性.

`plus`是无限格点上的全\(|+\rangle\)乘积态. 它只检查输入Pauli串的有限支撑: 若全部非恒等因子都是X, 返回1, 否则返回0. 已证明任意有限区域的重建矩阵等于相应乘积态矩阵, 因而全尺寸合法且平移不变. 同一条定理直接实例化到二维与三维晶格. 凸组合定理进一步给出这两个态之间的整个连续混合族.

`allOne`给出必要的反例. 对每个Pauli串都置\(f(P)=1\), 它归一化, 平移不变, 各个期望值都在\([-1,1]\)内, 但并不合法. Lean严格证明其单格点重建矩阵及负二次型为:

\[\rho=\begin{pmatrix}1&(1-i)/2\\(1+i)/2&0\end{pmatrix},\qquad v=(1,-1-i),\qquad v^\dagger\rho v=-1.\]

因此逐个检查Pauli期望值范围不能替代联合正性. 此反例采用精确复数运算, 不依赖浮点本征值.

## LLM提交候选时可以使用的接口

一个候选首先给出完整的`f : Correlator Γ`. 然后提交`IsLegal f`的证明, 需要平移不变时再证明`IsTranslationInvariant f`. 可以使用已有闭包定理组合候选, 也可以给出全尺寸因子分解, 或使用已实现的局域极限定理. 任何新构造仍需完成相应的普遍量化证明, Lean不会自动把某个求值程序视为合法态.

这些定义位于`noncomputable section`中, 涉及精确实数及经典数学. 当前成果是语义和证明接口, 不是已经可以对任意输入串高效求值的数值服务. 任意语义态是否有可计算描述, 以及能否有效证明其合法性, 均不是本轮已经解决的问题. MPS, PEPS或电路可作为候选来源, 接口没有把它们设成允许状态的白名单.

## 代码与复现

- `lean/QuantumManyBodyVariational/CorrelatorStates.lean`: 全局串, 关联函数, 归一化, 合法性, 平移不变性和全局极限接口.
- `lean/QuantumManyBodyVariational/InfiniteStateCertificates.lean`: 汇总入口, 期望值恢复, 因子分解证书, 二维和三维实例.
- `LocalPauli.lean`, `PauliRecovery.lean`, `LocalMarginals.lean`, `GlobalMarginals.lean`: 实际矩阵重建, 正交性和部分迹证明.
- `StateLimits.lean`, `StateClosure.lean`, `StateExamples.lean`, `StateCounterexample.lean`: 极限, 凸组合, 正面例子与反例.
- `lean/InfiniteStateAudit.lean`: 核心定理的公理依赖检查.

使用已有Lean 4.35.0, mathlib固定在`56a005855ea17e0b0958f73ec801ae140b0a1679`. 从Eureka根目录运行:

```bash
Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/check_infinite_states.py
```

脚本调用本课题已有工具链, 构建整个Lean库, 检查证明逃逸标记, 并核对核心定理的公理依赖. 允许的公理仅为Lean经典数学基础中的`propext`, `Classical.choice`, `Quot.sound`; 不接受自设物理公理或未完成证明. 每次生成独立的`tmp/lean-infinite-state-check-<UTC>/manifest.json`, 保存源文件哈希, 版本, 实际退出码及日志哈希. 验证期间不调用外部LLM/API.
