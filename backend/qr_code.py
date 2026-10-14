"""A small QR Code encoder (ISO/IEC 18004) for the two-step sign-in setup.

Enrolling an authenticator app means showing an otpauth:// URI as a QR code.
Neither stack had an encoder, and a QR library would be a new dependency in
the frontend bundle (or the backend image) for one screen. This is the
standard algorithm, byte mode only, error-correction level M, versions 1-20
(up to 666 bytes, far more than any otpauth URI). The structure follows
Project Nayuki's reference implementation (MIT); the tables are the standard's.

The output is a square matrix of booleans (True = dark), without the quiet
zone; the frontend draws it as an SVG. tests/test_qr_code.py decodes the
result with OpenCV's QR detector, so a mistake here fails a test rather than
an enrolment.
"""

from __future__ import annotations

from typing import List

MIN_VERSION, MAX_VERSION = 1, 20

# Error-correction level M, indexed by version (index 0 unused).
_ECC_CODEWORDS_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26)
_NUM_ERROR_CORRECTION_BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16)
_FORMAT_BITS_M = 0  # the two-bit level indicator: L=1, M=0, Q=3, H=2

_PENALTY_N1, _PENALTY_N2, _PENALTY_N3, _PENALTY_N4 = 3, 3, 40, 10


class QrError(ValueError):
    """The text does not fit in the largest supported version."""


def _num_raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        num_align = ver // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int) -> int:
    return _num_raw_data_modules(ver) // 8 - _ECC_CODEWORDS_PER_BLOCK[ver] * _NUM_ERROR_CORRECTION_BLOCKS[ver]


def _gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> List[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result


def _rs_remainder(data: List[int], divisor: List[int]) -> List[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_multiply(coef, factor)
    return result


def _alignment_positions(ver: int, size: int) -> List[int]:
    if ver == 1:
        return []
    num_align = ver // 7 + 2
    step = (ver * 8 + num_align * 3 + 5) // (num_align * 4 - 4) * 2
    result = [6]
    pos = size - 7
    for _ in range(num_align - 1):
        result.insert(1, pos)
        pos -= step
    return result


def _data_codewords(payload: bytes) -> tuple[int, List[int]]:
    """Pick the smallest version, then build the padded data codewords."""
    for ver in range(MIN_VERSION, MAX_VERSION + 1):
        count_bits = 8 if ver <= 9 else 16
        capacity = _num_data_codewords(ver) * 8
        if 4 + count_bits + 8 * len(payload) <= capacity:
            break
    else:
        raise QrError("text is too long for a QR code")
    bits: List[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)                      # byte mode
    put(len(payload), count_bits)
    for byte in payload:
        put(byte, 8)
    put(0, min(4, capacity - len(bits)))  # terminator
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return ver, codewords


def _with_ecc(ver: int, data: List[int]) -> List[int]:
    num_blocks = _NUM_ERROR_CORRECTION_BLOCKS[ver]
    ecc_len = _ECC_CODEWORDS_PER_BLOCK[ver]
    raw_codewords = _num_raw_data_modules(ver) // 8
    num_short = num_blocks - raw_codewords % num_blocks
    short_len = raw_codewords // num_blocks
    divisor = _rs_divisor(ecc_len)
    blocks: List[List[int]] = []
    k = 0
    for i in range(num_blocks):
        take = short_len - ecc_len + (0 if i < num_short else 1)
        dat = data[k:k + take]
        k += take
        ecc = _rs_remainder(dat, divisor)
        if i < num_short:
            dat = dat + [0]  # placeholder so every block has the same length
        blocks.append(dat + ecc)
    result: List[int] = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                result.append(block[i])
    return result


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self.set_function(x, y, dist not in (2, 4))
        positions = _alignment_positions(self.ver, size)
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_function(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set_function(a, b, bit)
                self.set_function(b, a, bit)

    def draw_format_bits(self, mask: int) -> None:
        data = _FORMAT_BITS_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        size = self.size
        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, bit(i))
        self.set_function(8, size - 8, True)  # the dark module

    def draw_codewords(self, data: List[int]) -> None:
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.dark[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.function[y][x]:
                    continue
                if mask == 0:
                    invert = (x + y) % 2 == 0
                elif mask == 1:
                    invert = y % 2 == 0
                elif mask == 2:
                    invert = x % 3 == 0
                elif mask == 3:
                    invert = (x + y) % 3 == 0
                elif mask == 4:
                    invert = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    invert = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    invert = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    invert = ((x + y) % 2 + x * y % 3) % 2 == 0
                if invert:
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        size, grid = self.size, self.dark
        score = 0
        lines = [row for row in grid] + [[grid[y][x] for y in range(size)] for x in range(size)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run = 1
            for k in range(1, size + 1):
                if k < size and line[k] == line[k - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += _PENALTY_N1 + run - 5
                run = 1
            for k in range(size - 10):
                window = line[k:k + 11]
                if window == finder_a or window == finder_b:
                    score += _PENALTY_N3
        for y in range(size - 1):
            for x in range(size - 1):
                colour = grid[y][x]
                if colour == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += _PENALTY_N2
        dark = sum(sum(1 for cell in row if cell) for row in grid)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + max(0, k) * _PENALTY_N4


def encode(text: str) -> List[List[bool]]:
    """The QR matrix for `text` (UTF-8, byte mode, level M). True = dark."""
    ver, data = _data_codewords(text.encode("utf-8"))
    codewords = _with_ecc(ver, data)
    best: _Matrix | None = None
    best_penalty = 0
    for mask in range(8):
        matrix = _Matrix(ver)
        matrix.draw_function_patterns()
        matrix.draw_codewords(codewords)
        matrix.apply_mask(mask)
        matrix.draw_format_bits(mask)
        score = matrix.penalty()
        if best is None or score < best_penalty:
            best, best_penalty = matrix, score
    assert best is not None
    return best.dark


def rows(text: str) -> List[str]:
    """The matrix as strings of '1' (dark) and '0', one per row — compact JSON."""
    return ["".join("1" if cell else "0" for cell in row) for row in encode(text)]
