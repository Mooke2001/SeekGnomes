# 共同的关联函数格式

一份`.state`源程序定义\(f:\mathrm{Word}\to\mathbb R\). 在SeekGnomes中, Constructor将完整源程序保存为`f.state`并交接. `Word`是无限晶格上的有限支撑Pauli串, 空串表示单位算符. 晶格维数和每原胞qubit数由任务给定.

例如, 下面定义均匀\(Z\)极化态.

```state
def local_value(p: Pauli) -> Real:
    return 1 if p == Z else 0

def f(P: Word) -> Real:
    return prod(map(local_value, labels(P)))
```

## 写法

源语言使用Python式缩进和类型注解. 程序经语法分析和类型检查后由解释器运行. 函数名在Lean中保留为具名定义, `if`汇合后的计算保留一次, `for`和有限聚合保留为按输入长度执行的循环.

| 用途 | 写法 |
| --- | --- |
| 函数与常量 | `def g(x: Int) -> Real:`, 类型标注的常量, 局部赋值和元组解包, 具名调用和`lambda` |
| 类型 | `Nat`, `Int`, `Rat`, `Real`, `Bool`, `Pauli`, `Word`, `Cell`, `Factor`, `List[T]`, `Tuple[A, B]`, `Fn[A, B]` |
| 算术 | `+`, `-`, `*`, `/`, `//`, `%`, `**`; `abs`, `min`, `max`; `sqrt`, `exp`, `log`, `sin`, `cos`, `tanh`, `pi`, `e` |
| 分支 | `if/elif/else`, 条件表达式, `match`与有限模式, `and/or/not`, 短路比较, `require(condition, value)` |
| 有限集合和序列计算 | `range`, `len`, 非负索引, 列表推导式, `for`, `map`, `filter`, `flat_map`, `zip`, `concat`, `fold(step, initial, xs)`, `sum`, `prod`, `all`, `any`, `contains(xs, x)`, `lookup(pairs, key, default)` |
| 晶格操作 | `factors(P)`, `labels(P)`, `factor.cell`, `factor.qubit`, `factor.pauli`, `Cell([...])`, `pauli_at(P, cell, qubit)`, `translate(P, cell)`, `factor(cell, qubit, pauli)`, `word_from_factors(xs)` |

`/`对精确整数和有理数产生有理数结果. 十进制字面量按其文本转为精确有理数. `//`和`%`要求正除数, `range`采用半开区间并要求步长非零. `factors(P)`按胞坐标字典序、再按胞内编号枚举, `pauli_at`在缺失位置返回`I`. `word_from_factors`要求格点各异, 并删除显式的`I`因子. `lookup`采用第一个匹配的键. 空和为0, 空积为1. 动态数值定义域或索引错误在实际使用时进入检查, Lean的固定目标包含数值前提.

在源程序的第一条非注释行写`use math`可使用统一的有限代数库. `Complex`表示实部虚部对, `Vector`和`Matrix`表示实数列表及其列表. 库含复数加减乘除、共轭和模平方, 向量内积和范数, Horner多项式求值, 实矩阵转置、乘法、迹、幂、单位矩阵和Kronecker积, 及复矩阵乘法、迹、伴随和Kronecker积. 编译器只发出实际调用到的库定义.

当控制表达式涉及实数, 比较按项目既有的确定性有理数规则自动执行. `--sqrt-bits`和`--series-terms`属于分支语义的配置, 进入产物清单. 实数分支的相等指按该规则得到的有理数相等. 数值取值沿另一条路径计算. 有理算术在资源允许时先精确计算, 最后转换为浮点数. 其他表达式由独立的mpmath区间上下文求值, 自动提高工作精度直到区间足够窄, 或在资源界内给出明确错误. 对绝对值至多1的结果, 区间路径将返回值与包围区间端点之间的误差控制在\(2^{-44}\). 该区间算法由数值软件运行, Lean端保留精确实数语义.

## 极限

```state
def approximant(P: Word, n: Nat) -> Real:
    return ...

def cutoff(P: Word, bits: Nat) -> Nat:
    return ...

def f(P: Word) -> Real:
    return limit(approximant, cutoff, P)
```

在极限形式中, Lean的`f`取自然数序列的精确极限. `LimitTail`要求对每个`P`, `bits`和每个不小于\(N=\mathrm{cutoff}(P,\mathrm{bits})\)的\(m\), 有\(\lvert f_m(P)-f_N(P)\rvert\le 2^{-\mathrm{bits}}\). Formalist需要证明这个尾界和态的合法性、晶胞平移不变性以及数值定义域前提. Python按所请求的截断位数计算第\(N\)项的浮点值. 这个位数表示截断误差目标, 浮点误差另计.

## 命令

在Eureka根目录可以构建产物并计算题库第13题的能量.

```bash
Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/state_format.py \
  build Projects/QuantumManyBodyVariational/examples/state_format/square_tfim.state \
  --dimension 2 --output Projects/QuantumManyBodyVariational/tmp/state-format-square-tfim-v1

Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/state_format.py \
  energy Projects/QuantumManyBodyVariational/examples/state_format/square_tfim.state --task-id 13

Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/check_state_format_lean.py
```

`build`生成`GeneratedState.lean`、可调用的`f.py`和包含源码、分支语义、运行时与产物摘要的`manifest.json`. `f.py`导出`f(word)`, 使用项目的解释器. Lean产物的固定证明目标是`GeneratedState.ProofObligation`. [迹态的证明](examples/state_format/TracialProof.lean)演示如何证明这个目标.

`energy`也接受`--task local.json`, 其中包含`dimension`、`qubits_per_cell`和每原胞的哈密顿量`terms`数组. 解释器只求值这些局域项涉及的不同Pauli串, 按\(\sum_j c_j f(P_j)/q\)给出每qubit能量密度和逐项贡献. 命令返回浮点估计. 态的合法性由对应的Lean证明核验.
