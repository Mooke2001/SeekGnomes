"""Finite algebra expressed in the shared source language."""

MATH_SOURCE = """\
def complex_number(re: Real, im: Real) -> Complex:
    return (re, im)

def complex_add(a: Complex, b: Complex) -> Complex:
    return (a[0] + b[0], a[1] + b[1])

def complex_sub(a: Complex, b: Complex) -> Complex:
    return (a[0] - b[0], a[1] - b[1])

def complex_mul(a: Complex, b: Complex) -> Complex:
    return (a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0])

def complex_conj(a: Complex) -> Complex:
    return (a[0], -a[1])

def complex_abs2(a: Complex) -> Real:
    return a[0] * a[0] + a[1] * a[1]

def complex_div(a: Complex, b: Complex) -> Complex:
    squared = complex_abs2(b)
    numerator = complex_mul(a, complex_conj(b))
    return (numerator[0] / squared, numerator[1] / squared)

def vector_dot(a: Vector, b: Vector) -> Real:
    return require(len(a) == len(b), sum([a[i] * b[i] for i in range(len(a))]))

def vector_norm(a: Vector) -> Real:
    return sqrt(vector_dot(a, a))

def polynomial(coefficients: Vector, x: Real) -> Real:
    return fold(
        lambda total, i: total * x + coefficients[i],
        Real(0),
        range(len(coefficients) - 1, -1, -1)
    )

def matrix_width(a: Matrix) -> Int:
    return 0 if len(a) == 0 else len(a[0])

def rectangular(a: Matrix) -> Bool:
    width = matrix_width(a)
    return all([len(row) == width for row in a])

def matrix_transpose(a: Matrix) -> Matrix:
    return require(rectangular(a), [[row[j] for row in a] for j in range(matrix_width(a))])

def matrix_multiply(a: Matrix, b: Matrix) -> Matrix:
    rows = matrix_transpose(b)
    return require(
        rectangular(a) and matrix_width(a) == len(b),
        [[vector_dot(row, col) for col in rows] for row in a]
    )

def matrix_trace(a: Matrix) -> Real:
    return require(
        rectangular(a) and matrix_width(a) == len(a),
        sum([a[i][i] for i in range(len(a))])
    )

def matrix_eye(n: Nat) -> Matrix:
    return [[Real(1) if i == j else Real(0) for j in range(n)] for i in range(n)]

def matrix_power(a: Matrix, n: Nat) -> Matrix:
    return require(
        rectangular(a) and matrix_width(a) == len(a),
        fold(lambda result, i: matrix_multiply(result, a), matrix_eye(Nat(len(a))), range(n))
    )

def kronecker(a: Matrix, b: Matrix) -> Matrix:
    return require(
        rectangular(a) and rectangular(b),
        [[x * y for x in row_a for y in row_b] for row_a in a for row_b in b]
    )

def complex_bilinear(a: List[Complex], b: List[Complex]) -> Complex:
    return require(
        len(a) == len(b),
        fold(lambda total, pair: complex_add(total, complex_mul(pair[0], pair[1])),
             complex_number(0, 0), zip(a, b))
    )

def complex_inner(a: List[Complex], b: List[Complex]) -> Complex:
    return complex_bilinear(map(complex_conj, a), b)

def complex_matrix_width(a: List[List[Complex]]) -> Int:
    return 0 if len(a) == 0 else len(a[0])

def complex_rectangular(a: List[List[Complex]]) -> Bool:
    width = complex_matrix_width(a)
    return all([len(row) == width for row in a])

def complex_matrix_transpose(a: List[List[Complex]]) -> List[List[Complex]]:
    return require(complex_rectangular(a), [[row[j] for row in a] for j in range(complex_matrix_width(a))])

def adjoint(a: List[List[Complex]]) -> List[List[Complex]]:
    return [map(complex_conj, row) for row in complex_matrix_transpose(a)]

def complex_matrix_multiply(a: List[List[Complex]], b: List[List[Complex]]) -> List[List[Complex]]:
    columns = complex_matrix_transpose(b)
    return require(
        complex_rectangular(a) and complex_matrix_width(a) == len(b),
        [[complex_bilinear(row, col) for col in columns] for row in a]
    )

def complex_matrix_trace(a: List[List[Complex]]) -> Complex:
    return require(
        complex_rectangular(a) and complex_matrix_width(a) == len(a),
        fold(lambda total, i: complex_add(total, a[i][i]), complex_number(0, 0), range(len(a)))
    )

def complex_kronecker(a: List[List[Complex]], b: List[List[Complex]]) -> List[List[Complex]]:
    return require(
        complex_rectangular(a) and complex_rectangular(b),
        [[complex_mul(x, y) for x in row_a for y in row_b] for row_a in a for row_b in b]
    )
"""
