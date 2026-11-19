# 实数取值与统一有理数分支

当前默认接口为hybrid. 取值表达式在Lean中保留理想实数运算, 所有显式逻辑控制条件采用统一的确定性有理数近似语义. Python数值求值与Lean定义使用相同的有理数分支, 不使用float大小关系决定控制流.

## 1. 两条计算路径

每个数值表达式有两个含义:

- 取值路径: 例如sqrt(2)在Lean中表示Real.sqrt(2), 数值端在需要返回值时近似求值.
- 分支路径: 同一表达式按确定的有理数算法重新解释, 例如sqrt(2)使用RationalMath.sqrtWith, 运算及比较都在有理数上精确完成.

这两条路径沿整个表达式及局部变量, 辅助函数和有限循环状态传播. 分支路径不是把取值路径已经舍入的float转换为Fraction.

```python
from math import sqrt

def f(P):
    x = sqrt(2)
    return x if x * x == 2 else 2 * x
```

这里有理数平方根近似r不满足r*r==2, 因此Python与Lean都选else. 所选表达式在Lean中仍为2*Real.sqrt(2), 没有被改成2*r. 示例只说明语言语义, 不声称该函数是合法态.

显式控制包括if, 条件表达式, 数值比较, and/or, all/any及生成器过滤条件. 整数坐标和Pauli标签等离散判断保持其精确语义. min/max和abs作为数值取值原语, 保留各自实数定义; 若其结果用于显式控制条件, 则相应条件使用其有理数路径.

## 2. 统一模块

统一分支模块为correlator_hybrid_branch.py, 有理数初等函数仍复用correlator_rational_math.py及Lean中的RationalMath. 支持有序比较和==/!=, 不要求数值间隔.

平方根网格位数bits和有限级数项数terms是固定配置, 默认40与32. 它们控制分支所采用的有理数函数, 因而是态定义的一部分. 改变配置可能改变分支和态, 必须重新生成Lean定义与证明绑定. 本规则不承诺有理数分支永远等同于原理想实数比较, 它直接规定有理数比较就是实际分支语义.

有理数函数的精度与范围说明见[有理数接口](PYTHON_CORRELATOR_RATIONAL.md). 当前不提供所有函数, 所有参数范围上的统一已认证误差界.

## 3. 纯表达式求值

Hybrid只接受纯函数子语言, 不执行源模块. 实数与有理数两个数值分量按实际依赖分别求值, 未使用的数值分量不强制求值. 布尔判断, 整数运算与容器构造仍按受支持的执行顺序进行, 逻辑条件遵守短路规则; 其使用条件也进入Lean证明目标. 因而仅用于分支的exp(1000)可以走有理数算法, 不因无关的float exp溢出失败; 仅用于实数取值的表达式也不会因其未使用的有理数路径出现零分母而失败.

这是一种数学表达式语义, 不等同于原生Python的逐句副作用或异常执行规则. 不支持文件, 外部状态, 随机数, 任意while, 一般递归或本征分解. 支持的辅助函数, 注解, 别名, 容器, 有限for和range等语法沿用扩展前端.

需要使用的数值路径若发生除零, 非法对数/平方根, 溢出或资源超限, 返回明确错误. 在Lean中生成对应的实数与有理数定义域条件, 与合法性及平移不变性一起作为证明义务.

## 4. Lean与能量接口

生成的f直接为实值Correlator. 支撑计算内部同时携带实数值, 有理数分支值及对应定义域条件. Lean里的比较读取有理数分支值, 最终f读取实数值. 因此证明对象就是包含这一分支规则的混合函数.

从Eureka根目录运行:

```bash
Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/python_correlator.py translate \
  --source Projects/QuantumManyBodyVariational/examples/python_correlator_hybrid/rational_branch_real_product.py \
  --dimension 2 --qubits-per-cell 1 --sqrt-bits 40 --series-terms 32 \
  --output Projects/QuantumManyBodyVariational/tmp/python-f-hybrid-demo/Candidate.lean

Environment/Python/run.sh Projects/QuantumManyBodyVariational/scripts/python_correlator.py energy \
  --source Projects/QuantumManyBodyVariational/examples/python_correlator_hybrid/rational_branch_real_product.py \
  --task-id 13 \
  --manifest Projects/QuantumManyBodyVariational/tmp/python-f-hybrid-demo/Candidate.manifest.json
```

能量按e(f)=sum(c_j*f(P_j))/q数值求值, 输出mode=approximate_float及逐项期望值和贡献. 数值能量没有自动附带严格误差界或认证变分上界; 理想实数值的数值近似与分支一致性是不同义务. 上述未带证明参数的调用以外部Lean证明已通过为前提, manifest匹配本身不是证明凭据.

通用证明入口现见[固定目标核验接口](HYBRID_PROOF_INTERFACE.md). `prepare-proof`生成宿主固定定义和证明模板, `check-proof`核实实际证明通过独立内核且对应固定原命题. `energy --proof Candidate.lean`在同一次调用中先完成核验, 仅在通过后计算能量.

## 5. 示例与兼容

rotation_product.py使用实数sin/cos描述均匀纯乘积态. rational_branch_real_product.py先按统一Q规则选取角度, 再以实数sin/cos构造乘积态. exact_identity_branch.py演示sqrt(2)平方等号在Q分支中为假, 因此选择Z极化态, Lean与Python的选择相同.

旧清单自动恢复其原v1, v2或rational语言及参数. 显式使用--language rational可以继续采用全有理数方案. 历史冻结测评数据不变, 新接口尚未改接历史测评agent工具. 示例的物理解释与Lean实际证明的范围以本轮验证报告分别标记.
