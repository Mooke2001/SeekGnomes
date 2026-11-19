# Writing a SeekGnomes `.state` candidate

Write one UTF-8 file named `f.state` containing the source code. The required
entry point is `def f(P: Word) -> Real:`. It returns the expectation of any
finite Pauli word in your proposed state on the task's infinite lattice.
Define it for the identity and for arbitrary finite supports, including
negative cell coordinates. The task sets the dimension, number of qubits per
cell, Pauli convention and Hamiltonian. Use the code inside a fenced example
when writing the file.

## A first candidate

The uniform product of `+Z` eigenstates gives 1 for an all-`Z` word and 0 if
any factor is `X` or `Y`. The empty product is 1, giving the identity its
required expectation.

```state
def f(P: Word) -> Real:
    return prod([1 if p == Z else 0 for p in labels(P)])
```

## Syntax and lattice operations

The language uses Python-style indentation. At the top level, write typed
functions with fixed positional arguments and typed constants such as
`mixing: Rat = 1 / 2`. In a function, assign local variables, unpack tuples,
or use `if/elif/else`, conditional expressions, `match`, and finite `for`
loops. Helpers can call other named helpers with an acyclic call graph.
For computation, use the provided operations in the table. Python imports,
classes, general recursion and `while` are outside the language.

| Need | Supported forms |
| --- | --- |
| Types | `Nat`, `Int`, `Rat`, `Real`, `Bool`, `Pauli`, `Word`, `Cell`, `Factor`, `List[T]`, `Tuple[A, B]`, `Fn[A, B]` |
| Finite sequences | Lists, comprehensions, indexing, `len(xs)`, `range(start, stop, step)`, `zip(a, b)`, `concat(a, b)`, `filter(pred, xs)`, `map(fn, xs)`, `flat_map(fn, xs)`, `fold(step, initial, xs)`. Use a named helper or a lambda with a sequence operator. |
| Reductions and lookup | `sum(xs)`, `prod(xs)`, `all(xs)`, `any(xs)`, `contains(xs, x)`, `lookup(pairs, key, default)` |
| Arithmetic | `+`, `-`, `*`, `/`, `//`, `%`, `**`, `abs(x)`, `min(a, b)`, `max(a, b)`, `sqrt(x)`, `exp(x)`, `log(x)`, `sin(x)`, `cos(x)`, `tanh(x)`, `pi()`, `e()` |
| Pauli words | `factors(P)`, `labels(P)`, `len(P)`, `pauli_at(P, c, q)`, `translate(P, delta)` |
| Lattice construction | `Cell([x0, x1, ...])`, `factor(c, q, p)`, `word_from_factors(fs)`, factor fields `.cell`, `.qubit`, `.pauli` |

- Use the unquoted Pauli constants `I`, `X`, `Y`, `Z`. A canonical input
  contains only its nonidentity factors. `factors(P)` sorts them by cell
  coordinates lexicographically, then by qubit index. `pauli_at` returns `I`
  at a valid site absent from the support.
- A `Cell` has exactly the task's number of coordinates. Qubits within each
  cell are indexed from zero. `word_from_factors` requires distinct sites and
  removes explicit identity factors. `translate` shifts the cell of every
  factor while preserving its qubit index.
- `/` on integers or rationals gives an exact rational. Decimal literals
  also denote exact rationals. `Real(x)` casts a numeric value explicitly.
  `Nat(i)` requires a nonnegative integer. Exponents must be nonnegative
  integers within the host's evaluation limit. Comparisons of `Real` expressions
  use the host's deterministic rational approximation, including equality.
  Use `Int` or `Rat` when an exact branch decision matters.
- Use valid nonnegative indices, with literal indices for tuples. Supply
  nonzero divisors for `/`, positive
  divisors for `//` and `%`, and a nonzero step for half-open `range`.
  `sqrt` requires a nonnegative argument and `log` requires a positive one.
  The fixed Lean goal includes the relevant numerical domain conditions.

## Coordinate-dependent constructions

To use a period-two construction along the first cell coordinate, average it
with its one-cell translate. Each contribution below is a staggered product
state. A cell shift permutes the two contributions, so their mixture is
translation invariant. The factor's qubit index is available for
constructions that distinguish qubits within a cell.

```state
def site_value(t: Factor, shift: Int) -> Real:
    if t.pauli != Z:
        return 0
    return 1 if (t.cell[0] + shift) % 2 == 0 else -1

def staggered(P: Word, shift: Int) -> Real:
    return prod([site_value(t, shift) for t in factors(P)])

def f(P: Word) -> Real:
    return (staggered(P, 0) + staggered(P, 1)) / 2
```

## Finite linear algebra

Put `use math` on the first non-comment, nonblank line to access the bundled
linear algebra library. `Complex` is a pair of reals, `Vector` is a list of
reals, and `Matrix` is a list of rows. Use rectangular matrices with compatible
dimensions for multiplication, and square matrices for traces and powers.
Available helpers include `complex_number`, `complex_mul`, `complex_conj`,
`vector_dot`, `matrix_multiply`, `matrix_trace`, `matrix_power`, and `adjoint`
for complex matrices.

This example uses the same one-qubit density matrix at every site.

```state
use math

def f(P: Word) -> Real:
    rho: Matrix = [[3 / 4, 0], [0, 1 / 4]]
    z: Matrix = [[1, 0], [0, -1]]
    magnetization = matrix_trace(matrix_multiply(rho, z))
    return prod([magnetization if p == Z else Real(0) for p in labels(P)])
```

## Limits with a proof of the tail bound

For a limit, make the body of `f` a single `return limit(approximant, cutoff, P)`.
Define the named helpers with the types shown below. Lean uses the exact
sequence limit. For each word `P` and nonnegative integer `bits`, the proof
must show that every approximation from index `N = cutoff(P, bits)` onward
differs from the approximation at `N` by at most `2^(-bits)`. Prove the
domain conditions for both helpers on all inputs. The evaluator uses the
term at the requested cutoff for numerical scoring.

```state
def polarized(P: Word, spin: Real) -> Real:
    return prod([spin if p == Z else Real(0) for p in labels(P)])

def approximant(P: Word, n: Nat) -> Real:
    weight = Real(1 / 2) ** n
    return (1 - weight) * polarized(P, 1) + weight * polarized(P, -1)

def cutoff(P: Word, bits: Nat) -> Nat:
    return bits + Nat(1)

def f(P: Word) -> Real:
    return limit(approximant, cutoff, P)
```

Here the mixture weight is `2^(-n)`. The two product-state expectations
differ by at most 2. For `m >= N`, the difference between the weights is at
most `2^(-N)`, so `N = bits + 1` gives the required bound.

## Handoff and proof

- **Constructor.** Save the complete definition as `f.state` and call
  `benchmark_action` with the arguments below. Use `notes` to tell Formalist
  how you plan to justify normalization, positivity and translation invariance,
  plus any domain or tail conditions. If you change the construction or any
  parameter, submit the complete revised file in a new handoff.

  ```json
  {"tool":"create_handoff","arguments":{"definition_path":"f.state","notes":"Uniform product of +Z eigenstates."}}
  ```

- **Formalist.** Read `reference/handoff/f.state` and call `prepare {}`.
  Read the generated definition and template using the paths in
  `generated_paths`. Write `Submission.accepted : GeneratedState.ProofObligation`
  in `Candidate.lean`. Use `run_lean {proof_path:"Candidate.lean"}` for proof
  diagnostics and `score {proof_path:"Candidate.lean"}` for verification and
  scoring. The host evaluates the same frozen source after the proof passes and
  returns the floating-point energy per qubit to Constructor.
