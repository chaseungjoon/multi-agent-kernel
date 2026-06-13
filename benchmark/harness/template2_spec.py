"""Specification for ``project_template_2`` — the large benchmark target.

This is the single source of truth for the ~90-operation ``toolkit`` library that
``project_template_2`` is built from. Each :class:`OpSpec` carries the *reference*
implementation (used by the mock backend and to verify the suite), a one-line
docstring (what the real agent sees), and its test cases. The on-disk template
(stub modules + tests) is generated from this file by ``tools/gen_template2.py``,
so the stubs, references, and tests can never drift apart.

The functions are modelled on the kinds of utilities found in real open-source
libraries (string/number/sequence/dict/date/math/parsing/set/encoding helpers, in
the spirit of ``boltons`` / ``more-itertools`` / ``toolz``) so the workload is both
large and genuinely varied — not 90 trivial one-liners.
"""

from __future__ import annotations

from dataclasses import dataclass


def _s(text: str) -> str:
    """Normalise a flush-left triple-quoted function source: strip framing newlines."""
    return text.strip("\n") + "\n"


@dataclass(frozen=True)
class OpSpec:
    """One operation: its module, name, reference source, docstring, and test cases."""

    module: str
    name: str
    source: str  # full reference function source (def header + body)
    doc: str  # one-line docstring placed in the stub the agent implements
    cases: tuple  # ((args_tuple, expected), ...) — at least one
    raises: tuple = ()  # (args_tuple, ...) expected to raise ValueError

    @property
    def header(self) -> str:
        """The ``def ...:`` line, taken from the reference source."""
        return self.source.splitlines()[0]


OPS: list[OpSpec] = [
    # ===================== strkit — string utilities =====================
    OpSpec("strkit", "camel_to_snake", _s("""
def camel_to_snake(s: str) -> str:
    out = []
    for i, c in enumerate(s):
        if c.isupper() and i > 0:
            out.append("_")
        out.append(c.lower())
    return "".join(out)
"""), 'Convert camelCase to snake_case, e.g. "fooBarBaz" -> "foo_bar_baz".',
        cases=((("fooBarBaz",), "foo_bar_baz"), (("plain",), "plain"))),

    OpSpec("strkit", "snake_to_camel", _s("""
def snake_to_camel(s: str) -> str:
    parts = s.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])
"""), 'Convert snake_case to camelCase, e.g. "foo_bar_baz" -> "fooBarBaz".',
        cases=((("foo_bar_baz",), "fooBarBaz"), (("single",), "single"))),

    OpSpec("strkit", "truncate", _s("""
def truncate(s: str, n: int) -> str:
    if n < 3:
        raise ValueError("n must be at least 3")
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."
"""), 'Truncate s to at most n chars, using a trailing "..." when shortened (n>=3).',
        cases=((("hello world", 8), "hello..."), (("hi", 8), "hi")),
        raises=[("x", 2)]),

    OpSpec("strkit", "word_wrap", _s("""
def word_wrap(s: str, width: int) -> list:
    if width <= 0:
        raise ValueError("width must be positive")
    lines = []
    cur = ""
    for w in s.split():
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
"""), "Greedily wrap whitespace-separated words into lines of at most width chars.",
        cases=((("a bb ccc dddd", 5), ["a bb", "ccc", "dddd"]),),
        raises=[("hi", 0)]),

    OpSpec("strkit", "levenshtein", _s("""
def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
"""), "Return the Levenshtein edit distance between strings a and b.",
        cases=((("kitten", "sitting"), 3), (("abc", "abc"), 0))),

    OpSpec("strkit", "longest_common_prefix", _s("""
def longest_common_prefix(strs: list) -> str:
    if not strs:
        return ""
    prefix = strs[0]
    for s in strs[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
    return prefix
"""), "Return the longest common leading prefix of a list of strings.",
        cases=(((["flower", "flow", "flight"],), "fl"), ((["a", "b"],), ""))),

    OpSpec("strkit", "is_anagram", _s("""
def is_anagram(a: str, b: str) -> bool:
    na = sorted(c.lower() for c in a if c.isalnum())
    nb = sorted(c.lower() for c in b if c.isalnum())
    return na == nb
"""), "Whether a and b are anagrams, ignoring case and non-alphanumeric characters.",
        cases=((("Listen", "Silent"), True), (("foo", "bar"), False))),

    OpSpec("strkit", "title_case", _s("""
def title_case(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split())
"""), 'Capitalise the first letter of each word, e.g. "hello WORLD" -> "Hello World".',
        cases=((("hello WORLD",), "Hello World"),)),

    OpSpec("strkit", "count_substring", _s("""
def count_substring(s: str, sub: str) -> int:
    if not sub:
        return 0
    return s.count(sub)
"""), "Count non-overlapping occurrences of sub in s (0 if sub is empty).",
        cases=((("ababab", "ab"), 3), (("aaa", "aa"), 1))),

    OpSpec("strkit", "rot13", _s("""
def rot13(s: str) -> str:
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr((ord(c) - 97 + 13) % 26 + 97))
        elif "A" <= c <= "Z":
            out.append(chr((ord(c) - 65 + 13) % 26 + 65))
        else:
            out.append(c)
    return "".join(out)
"""), "Apply the ROT13 substitution cipher to the letters of s.",
        cases=((("Hello",), "Uryyb"),)),

    # ===================== numkit — numeric utilities =====================
    OpSpec("numkit", "is_perfect_square", _s("""
def is_perfect_square(n: int) -> bool:
    if n < 0:
        return False
    r = int(n ** 0.5)
    while r * r < n:
        r += 1
    while r * r > n:
        r -= 1
    return r * r == n
"""), "Whether the non-negative integer n is a perfect square.",
        cases=(((16,), True), ((15,), False), ((0,), True))),

    OpSpec("numkit", "prime_factors", _s("""
def prime_factors(n: int) -> list:
    if n < 2:
        return []
    factors = []
    d = 2
    x = n
    while d * d <= x:
        while x % d == 0:
            factors.append(d)
            x //= d
        d += 1
    if x > 1:
        factors.append(x)
    return factors
"""), "Return the prime factors of n in ascending order, with multiplicity.",
        cases=(((12,), [2, 2, 3]), ((13,), [13]), ((1,), []))),

    OpSpec("numkit", "nth_prime", _s("""
def nth_prime(k: int) -> int:
    if k < 1:
        raise ValueError("k must be at least 1")
    count = 0
    cand = 1
    while count < k:
        cand += 1
        is_p = True
        i = 2
        while i * i <= cand:
            if cand % i == 0:
                is_p = False
                break
            i += 1
        if is_p:
            count += 1
    return cand
"""), "Return the k-th prime (k>=1; nth_prime(1) == 2).",
        cases=(((1,), 2), ((5,), 11)),
        raises=[(0,)]),

    OpSpec("numkit", "collatz_steps", _s("""
def collatz_steps(n: int) -> int:
    if n < 1:
        raise ValueError("n must be at least 1")
    steps = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps
"""), "Number of Collatz steps to reach 1 from n (n>=1).",
        cases=(((1,), 0), ((6,), 8)),
        raises=[(0,)]),

    OpSpec("numkit", "gcd_many", _s("""
def gcd_many(nums: list) -> int:
    if not nums:
        raise ValueError("nums must be non-empty")
    result = abs(nums[0])
    for x in nums[1:]:
        a, b = result, abs(x)
        while b:
            a, b = b, a % b
        result = a
    return result
"""), "Greatest common divisor of a non-empty list of integers.",
        cases=((([12, 18, 24],), 6), (([7],), 7)),
        raises=[([],)]),

    OpSpec("numkit", "lcm_many", _s("""
def lcm_many(nums: list) -> int:
    if not nums:
        raise ValueError("nums must be non-empty")
    result = abs(nums[0])
    for x in nums[1:]:
        x = abs(x)
        if result == 0 or x == 0:
            result = 0
            continue
        a, b = result, x
        while b:
            a, b = b, a % b
        result = result * x // a
    return result
"""), "Least common multiple of a non-empty list of integers.",
        cases=((([4, 6, 8],), 24), (([5],), 5)),
        raises=[([],)]),

    OpSpec("numkit", "base_convert", _s("""
def base_convert(n: int, base: int) -> str:
    if not 2 <= base <= 36:
        raise ValueError("base must be between 2 and 36")
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        out.append(digits[n % base])
        n //= base
    return "".join(reversed(out))
"""), "Render non-negative n in the given base (2-36) with lower-case digits.",
        cases=(((255, 16), "ff"), ((10, 2), "1010")),
        raises=[(5, 1)]),

    OpSpec("numkit", "clamp", _s("""
def clamp(x: int, lo: int, hi: int) -> int:
    if lo > hi:
        raise ValueError("lo must be <= hi")
    return max(lo, min(x, hi))
"""), "Constrain x to the inclusive range [lo, hi]; raise if lo > hi.",
        cases=(((5, 0, 10), 5), ((-3, 0, 10), 0)),
        raises=[(1, 5, 0)]),

    OpSpec("numkit", "divisors", _s("""
def divisors(n: int) -> list:
    if n < 1:
        raise ValueError("n must be positive")
    divs = []
    i = 1
    while i * i <= n:
        if n % i == 0:
            divs.append(i)
            if i != n // i:
                divs.append(n // i)
        i += 1
    return sorted(divs)
"""), "Return all positive divisors of n in ascending order (n>=1).",
        cases=(((12,), [1, 2, 3, 4, 6, 12]), ((7,), [1, 7])),
        raises=[(0,)]),

    OpSpec("numkit", "mean", _s("""
def mean(nums: list) -> float:
    if not nums:
        raise ValueError("nums must be non-empty")
    return sum(nums) / len(nums)
"""), "Arithmetic mean of a non-empty list of numbers.",
        cases=((([1, 2, 3, 4],), 2.5), (([5],), 5.0)),
        raises=[([],)]),

    # ===================== seqkit — sequence utilities =====================
    OpSpec("seqkit", "windowed", _s("""
def windowed(items: list, size: int) -> list:
    if size <= 0:
        raise ValueError("size must be positive")
    if size > len(items):
        return []
    return [items[i:i + size] for i in range(len(items) - size + 1)]
"""), "All consecutive sliding windows of the given size over items.",
        cases=((([1, 2, 3, 4], 2), [[1, 2], [2, 3], [3, 4]]), (([1], 2), [])),
        raises=[([1, 2], 0)]),

    OpSpec("seqkit", "chunk", _s("""
def chunk(items: list, size: int) -> list:
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]
"""), "Split items into consecutive chunks of the given size (last may be shorter).",
        cases=((([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]),),
        raises=[([1], 0)]),

    OpSpec("seqkit", "flatten_deep", _s("""
def flatten_deep(nested: list) -> list:
    result = []
    for x in nested:
        if isinstance(x, list):
            result.extend(flatten_deep(x))
        else:
            result.append(x)
    return result
"""), "Recursively flatten arbitrarily nested lists into a single flat list.",
        cases=((([1, [2, [3, 4]], 5],), [1, 2, 3, 4, 5]),)),

    OpSpec("seqkit", "rotate", _s("""
def rotate(items: list, k: int) -> list:
    if not items:
        return []
    k %= len(items)
    return items[k:] + items[:k]
"""), "Rotate items left by k positions (negative k rotates right).",
        cases=((([1, 2, 3, 4, 5], 2), [3, 4, 5, 1, 2]), (([1, 2, 3], -1), [3, 1, 2]))),

    OpSpec("seqkit", "pairwise", _s("""
def pairwise(items: list) -> list:
    return [(items[i], items[i + 1]) for i in range(len(items) - 1)]
"""), "Return consecutive overlapping pairs (a,b),(b,c),... as a list of tuples.",
        cases=((([1, 2, 3],), [(1, 2), (2, 3)]), (([1],), []))),

    OpSpec("seqkit", "dedupe", _s("""
def dedupe(items: list) -> list:
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
"""), "Order-preserving removal of duplicate elements.",
        cases=((([1, 1, 2, 3, 2],), [1, 2, 3]),)),

    OpSpec("seqkit", "frequencies", _s("""
def frequencies(items: list) -> dict:
    counts = {}
    for x in items:
        counts[x] = counts.get(x, 0) + 1
    return counts
"""), "Map each distinct element to the number of times it appears.",
        cases=(((["a", "b", "a"],), {"a": 2, "b": 1}),)),

    OpSpec("seqkit", "partition_even_odd", _s("""
def partition_even_odd(nums: list) -> tuple:
    evens = [x for x in nums if x % 2 == 0]
    odds = [x for x in nums if x % 2 != 0]
    return (evens, odds)
"""), "Split integers into (evens, odds), preserving order within each group.",
        cases=((([1, 2, 3, 4],), ([2, 4], [1, 3])),)),

    OpSpec("seqkit", "interleave", _s("""
def interleave(a: list, b: list) -> list:
    out = []
    i = 0
    while i < len(a) or i < len(b):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
        i += 1
    return out
"""), "Interleave two lists element by element, appending any leftover tail.",
        cases=((([1, 3], [2, 4]), [1, 2, 3, 4]), (([1], [2, 3, 4]), [1, 2, 3, 4]))),

    OpSpec("seqkit", "run_length", _s("""
def run_length(items: list) -> list:
    out = []
    for x in items:
        if out and out[-1][0] == x:
            out[-1] = (x, out[-1][1] + 1)
        else:
            out.append((x, 1))
    return out
"""), "Run-length encode a list into (value, run_length) pairs.",
        cases=(((["a", "a", "b"],), [("a", 2), ("b", 1)]),)),

    # ===================== dictkit — mapping utilities =====================
    OpSpec("dictkit", "invert", _s("""
def invert(d: dict) -> dict:
    return {v: k for k, v in d.items()}
"""), "Return a new dict mapping each value back to its key.",
        cases=((({"a": 1, "b": 2},), {1: "a", 2: "b"}),)),

    OpSpec("dictkit", "merge", _s("""
def merge(a: dict, b: dict) -> dict:
    result = dict(a)
    result.update(b)
    return result
"""), "Shallow-merge two dicts into a new one; keys in b win.",
        cases=((({"a": 1}, {"b": 2, "a": 3}), {"a": 3, "b": 2}),)),

    OpSpec("dictkit", "pick", _s("""
def pick(d: dict, keys: list) -> dict:
    return {k: d[k] for k in keys if k in d}
"""), "Return a new dict with only the given keys that exist in d.",
        cases=((({"a": 1, "b": 2, "c": 3}, ["a", "c", "x"]), {"a": 1, "c": 3}),)),

    OpSpec("dictkit", "omit", _s("""
def omit(d: dict, keys: list) -> dict:
    return {k: v for k, v in d.items() if k not in keys}
"""), "Return a new dict without the given keys.",
        cases=((({"a": 1, "b": 2, "c": 3}, ["b"]), {"a": 1, "c": 3}),)),

    OpSpec("dictkit", "get_in", _s("""
def get_in(d: dict, path: list) -> object:
    cur = d
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur
"""), "Follow a list of keys into nested dicts; return None if any step is missing.",
        cases=((({"a": {"b": {"c": 5}}}, ["a", "b", "c"]), 5),
               (({"a": 1}, ["x"]), None))),

    OpSpec("dictkit", "key_of_max", _s("""
def key_of_max(d: dict) -> object:
    if not d:
        raise ValueError("dict must be non-empty")
    return max(d, key=lambda k: d[k])
"""), "Return the key whose value is largest (non-empty dict).",
        cases=((({"a": 1, "b": 9, "c": 3},), "b"),),
        raises=[({},)]),

    OpSpec("dictkit", "count_values", _s("""
def count_values(d: dict) -> dict:
    counts = {}
    for v in d.values():
        counts[v] = counts.get(v, 0) + 1
    return counts
"""), "Count how many keys map to each distinct value.",
        cases=((({"a": 1, "b": 1, "c": 2},), {1: 2, 2: 1}),)),

    OpSpec("dictkit", "deep_keys", _s("""
def deep_keys(d: dict) -> list:
    keys = []
    for k, v in d.items():
        keys.append(k)
        if isinstance(v, dict):
            keys.extend(deep_keys(v))
    return sorted(keys)
"""), "Return all keys, recursing into nested dicts, sorted ascending.",
        cases=((({"a": 1, "b": {"c": 2, "d": 3}},), ["a", "b", "c", "d"]),)),

    OpSpec("dictkit", "zip_dict", _s("""
def zip_dict(keys: list, values: list) -> dict:
    return dict(zip(keys, values))
"""), "Build a dict from parallel keys and values lists.",
        cases=(((["a", "b"], [1, 2]), {"a": 1, "b": 2}),)),

    OpSpec("dictkit", "max_value", _s("""
def max_value(d: dict) -> object:
    if not d:
        raise ValueError("dict must be non-empty")
    return max(d.values())
"""), "Return the largest value in a non-empty dict.",
        cases=((({"a": 1, "b": 5},), 5),),
        raises=[({},)]),

    # ===================== datekit — calendar math (no imports) ===========
    OpSpec("datekit", "is_leap_year", _s("""
def is_leap_year(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
"""), "Whether y is a leap year in the proleptic Gregorian calendar.",
        cases=(((2000,), True), ((1900,), False), ((2024,), True))),

    OpSpec("datekit", "days_in_month", _s("""
def days_in_month(y: int, m: int) -> int:
    if not 1 <= m <= 12:
        raise ValueError("month must be 1-12")
    lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    d = lengths[m - 1]
    if m == 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        d = 29
    return d
"""), "Number of days in month m of year y (1-12).",
        cases=(((2024, 2), 29), ((2023, 2), 28), ((2024, 4), 30)),
        raises=[(2024, 13)]),

    OpSpec("datekit", "day_of_year", _s("""
def day_of_year(y: int, m: int, d: int) -> int:
    lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0):
        lengths[1] = 29
    return sum(lengths[:m - 1]) + d
"""), "Ordinal day-of-year (1-366) for the date y-m-d.",
        cases=(((2024, 3, 1), 61), ((2023, 1, 1), 1))),

    OpSpec("datekit", "weekday", _s("""
def weekday(y: int, m: int, d: int) -> int:
    t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4]
    if m < 3:
        y -= 1
    return (y + y // 4 - y // 100 + y // 400 + t[m - 1] + d) % 7
"""), "Weekday of date y-m-d via Sakamoto's algorithm: 0=Sunday .. 6=Saturday.",
        cases=(((2024, 1, 1), 1), ((2000, 1, 1), 6))),

    OpSpec("datekit", "is_valid_date", _s("""
def is_valid_date(y: int, m: int, d: int) -> bool:
    if m < 1 or m > 12:
        return False
    lengths = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    dim = lengths[m - 1]
    if m == 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        dim = 29
    return 1 <= d <= dim
"""), "Whether y-m-d is a valid calendar date.",
        cases=(((2024, 2, 29), True), ((2023, 2, 29), False), ((2024, 13, 1), False))),

    OpSpec("datekit", "quarter_of", _s("""
def quarter_of(m: int) -> int:
    if not 1 <= m <= 12:
        raise ValueError("month must be 1-12")
    return (m - 1) // 3 + 1
"""), "Calendar quarter (1-4) containing month m.",
        cases=(((1,), 1), ((4,), 2), ((12,), 4)),
        raises=[(0,)]),

    OpSpec("datekit", "days_until_year_end", _s("""
def days_until_year_end(y: int, m: int, d: int) -> int:
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    lengths = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    doy = sum(lengths[:m - 1]) + d
    total = 366 if leap else 365
    return total - doy
"""), "Number of days from y-m-d to the end of that year.",
        cases=(((2024, 12, 31), 0), ((2023, 1, 1), 364))),

    OpSpec("datekit", "month_name", _s("""
def month_name(m: int) -> str:
    names = ["January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]
    if not 1 <= m <= 12:
        raise ValueError("month must be 1-12")
    return names[m - 1]
"""), "English name of month m (1-12).",
        cases=(((1,), "January"), ((12,), "December")),
        raises=[(0,)]),

    OpSpec("datekit", "season", _s("""
def season(m: int) -> str:
    if not 1 <= m <= 12:
        raise ValueError("month must be 1-12")
    if m in (12, 1, 2):
        return "winter"
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    return "autumn"
"""), "Northern-hemisphere season for month m (winter/spring/summer/autumn).",
        cases=(((1,), "winter"), ((4,), "spring"), ((7,), "summer"), ((10,), "autumn")),
        raises=[(13,)]),

    OpSpec("datekit", "age_in_years", _s("""
def age_in_years(by: int, bm: int, bd: int, y: int, m: int, d: int) -> int:
    return y - by - ((m, d) < (bm, bd))
"""), "Whole years from birth date (by,bm,bd) to reference date (y,m,d).",
        cases=((((2000, 6, 15, 2024, 6, 14), 23)),
               (((2000, 6, 15, 2024, 6, 15), 24)))),

    # ===================== mathkit — more math =====================
    OpSpec("mathkit", "fib", _s("""
def fib(n: int) -> int:
    if n < 0:
        raise ValueError("n must be non-negative")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
"""), "n-th Fibonacci number (fib(0)=0, fib(1)=1); raise ValueError if n<0.",
        cases=(((10,), 55), ((0,), 0)),
        raises=[(-1,)]),

    OpSpec("mathkit", "factorial", _s("""
def factorial(n: int) -> int:
    if n < 0:
        raise ValueError("n must be non-negative")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result
"""), "n! for non-negative n (0! == 1); raise ValueError if n<0.",
        cases=(((5,), 120), ((0,), 1)),
        raises=[(-1,)]),

    OpSpec("mathkit", "binomial", _s("""
def binomial(n: int, k: int) -> int:
    if k < 0 or n < 0 or k > n:
        return 0
    k = min(k, n - k)
    num = 1
    for i in range(k):
        num = num * (n - i) // (i + 1)
    return num
"""), "Binomial coefficient C(n, k); 0 when out of range.",
        cases=(((5, 2), 10), ((6, 0), 1))),

    OpSpec("mathkit", "sum_digits", _s("""
def sum_digits(n: int) -> int:
    return sum(int(c) for c in str(abs(n)))
"""), "Sum of the decimal digits of abs(n).",
        cases=(((1234,), 10), ((-9,), 9))),

    OpSpec("mathkit", "digital_root", _s("""
def digital_root(n: int) -> int:
    n = abs(n)
    while n >= 10:
        n = sum(int(c) for c in str(n))
    return n
"""), "Repeated digit sum of abs(n) until a single digit remains.",
        cases=(((9875,), 2), ((0,), 0))),

    OpSpec("mathkit", "is_armstrong", _s("""
def is_armstrong(n: int) -> bool:
    s = str(n)
    p = len(s)
    return n == sum(int(c) ** p for c in s)
"""), "Whether n equals the sum of its digits each raised to the digit count.",
        cases=(((153,), True), ((154,), False))),

    OpSpec("mathkit", "triangular", _s("""
def triangular(n: int) -> int:
    if n < 0:
        raise ValueError("n must be non-negative")
    return n * (n + 1) // 2
"""), "n-th triangular number 0+1+...+n (n>=0).",
        cases=(((5,), 15), ((0,), 0)),
        raises=[(-1,)]),

    OpSpec("mathkit", "sum_primes_below", _s("""
def sum_primes_below(n: int) -> int:
    if n < 2:
        return 0
    sieve = [True] * n
    sieve[0] = sieve[1] = False
    for i in range(2, int(n ** 0.5) + 1):
        if sieve[i]:
            for j in range(i * i, n, i):
                sieve[j] = False
    return sum(i for i in range(n) if sieve[i])
"""), "Sum of all primes strictly below n.",
        cases=(((10,), 17), ((2,), 0))),

    OpSpec("mathkit", "power_mod", _s("""
def power_mod(b: int, e: int, m: int) -> int:
    if m <= 0:
        raise ValueError("modulus must be positive")
    if e < 0:
        raise ValueError("exponent must be non-negative")
    result = 1
    b %= m
    while e > 0:
        if e & 1:
            result = result * b % m
        e >>= 1
        b = b * b % m
    return result
"""), "Modular exponentiation (b**e) % m by fast squaring.",
        cases=(((2, 10, 1000), 24), ((3, 0, 7), 1)),
        raises=[(2, 2, 0)]),

    OpSpec("mathkit", "is_prime", _s("""
def is_prime(n: int) -> bool:
    if n < 2:
        return False
    i = 2
    while i * i <= n:
        if n % i == 0:
            return False
        i += 1
    return True
"""), "Whether n is a prime number.",
        cases=(((7,), True), ((9,), False), ((2,), True))),

    # ===================== parsekit — small parsers =====================
    OpSpec("parsekit", "parse_csv_line", _s("""
def parse_csv_line(line: str) -> list:
    return line.split(",")
"""), 'Split a simple comma-separated line (no quoting) into fields.',
        cases=((("a,b,c",), ["a", "b", "c"]), (("",), [""]))),

    OpSpec("parsekit", "parse_query_string", _s("""
def parse_query_string(qs: str) -> dict:
    result = {}
    if not qs:
        return {}
    for pair in qs.split("&"):
        k, _, v = pair.partition("=")
        result[k] = v
    return result
"""), 'Parse "a=1&b=2" into {"a":"1","b":"2"} (missing value -> "").',
        cases=((("a=1&b=2",), {"a": "1", "b": "2"}), (("x",), {"x": ""}))),

    OpSpec("parsekit", "tokenize_words", _s("""
def tokenize_words(s: str) -> list:
    words = []
    cur = []
    for c in s:
        if c.isalnum():
            cur.append(c.lower())
        elif cur:
            words.append("".join(cur))
            cur = []
    if cur:
        words.append("".join(cur))
    return words
"""), "Split text into lower-cased alphanumeric word tokens.",
        cases=((("Hello, World!",), ["hello", "world"]),)),

    OpSpec("parsekit", "parse_kv", _s("""
def parse_kv(s: str) -> dict:
    result = {}
    if not s:
        return {}
    for part in s.split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            result[k.strip()] = v.strip()
    return result
"""), 'Parse "k1:v1;k2:v2" into a dict, stripping whitespace around keys/values.',
        cases=((("a:1; b:2",), {"a": "1", "b": "2"}),)),

    OpSpec("parsekit", "balanced_brackets", _s("""
def balanced_brackets(s: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for c in s:
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack.pop() != pairs[c]:
                return False
    return not stack
"""), "Whether (), [], {} brackets in s are correctly balanced and nested.",
        cases=((("([]{})",), True), (("([)]",), False), (("(",), False))),

    OpSpec("parsekit", "roman_to_int", _s("""
def roman_to_int(s: str) -> int:
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for c in reversed(s):
        v = vals[c]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total
"""), "Convert a Roman numeral string to its integer value.",
        cases=((("IV",), 4), (("MCMXCIV",), 1994))),

    OpSpec("parsekit", "int_to_roman", _s("""
def int_to_roman(n: int) -> str:
    if not 1 <= n <= 3999:
        raise ValueError("n must be between 1 and 3999")
    table = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
             (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
             (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, sym in table:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)
"""), "Convert an integer (1-3999) to its Roman numeral string.",
        cases=((((4,), "IV"), ((1994,), "MCMXCIV"))),
        raises=[(0,)]),

    OpSpec("parsekit", "parse_bool", _s("""
def parse_bool(s: str) -> bool:
    t = s.strip().lower()
    if t in ("true", "yes", "1", "on"):
        return True
    if t in ("false", "no", "0", "off"):
        return False
    raise ValueError("cannot parse boolean: " + s)
"""), "Parse a boolean from true/yes/1/on vs false/no/0/off (case-insensitive).",
        cases=((("Yes",), True), ((" off ",), False)),
        raises=[("maybe",)]),

    OpSpec("parsekit", "parse_version", _s("""
def parse_version(s: str) -> tuple:
    return tuple(int(p) for p in s.split("."))
"""), 'Parse a dotted version "1.2.3" into a tuple of ints (1, 2, 3).',
        cases=((("1.2.3",), (1, 2, 3)), (("10.0",), (10, 0)))),

    OpSpec("parsekit", "parse_range", _s("""
def parse_range(s: str) -> list:
    a, _, b = s.partition("-")
    start = int(a)
    end = int(b)
    if start > end:
        raise ValueError("start must be <= end")
    return list(range(start, end + 1))
"""), 'Expand a "start-end" range into the inclusive list of integers.',
        cases=((("1-5",), [1, 2, 3, 4, 5]), (("3-3",), [3])),
        raises=[("5-1",)]),

    # ===================== setkit — set utilities =====================
    OpSpec("setkit", "jaccard", _s("""
def jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)
"""), "Jaccard similarity |A∩B| / |A∪B| of two collections (1.0 if both empty).",
        cases=((([1, 2, 3], [2, 3, 4]), 0.5), (([], []), 1.0))),

    OpSpec("setkit", "symmetric_diff", _s("""
def symmetric_diff(a: list, b: list) -> list:
    return sorted(set(a) ^ set(b))
"""), "Sorted symmetric difference: elements in exactly one of a or b.",
        cases=((([1, 2, 3], [2, 3, 4]), [1, 4]),)),

    OpSpec("setkit", "is_subset", _s("""
def is_subset(a: list, b: list) -> bool:
    return set(a) <= set(b)
"""), "Whether every element of a is also in b.",
        cases=((([1, 2], [1, 2, 3]), True), (([1, 4], [1, 2, 3]), False))),

    OpSpec("setkit", "union_all", _s("""
def union_all(lists: list) -> list:
    result = set()
    for lst in lists:
        result |= set(lst)
    return sorted(result)
"""), "Sorted union of all elements across a list of lists.",
        cases=((([[1, 2], [2, 3], [3, 4]],), [1, 2, 3, 4]),)),

    OpSpec("setkit", "intersection_all", _s("""
def intersection_all(lists: list) -> list:
    if not lists:
        return []
    result = set(lists[0])
    for lst in lists[1:]:
        result &= set(lst)
    return sorted(result)
"""), "Sorted intersection common to every list (empty if no lists).",
        cases=((([[1, 2, 3], [2, 3, 4], [3, 4, 5]],), [3]), (([],), []))),

    OpSpec("setkit", "count_common", _s("""
def count_common(a: list, b: list) -> int:
    return len(set(a) & set(b))
"""), "Number of distinct elements present in both a and b.",
        cases=((([1, 2, 3], [2, 3, 4]), 2),)),

    OpSpec("setkit", "unique_to_first", _s("""
def unique_to_first(a: list, b: list) -> list:
    return sorted(set(a) - set(b))
"""), "Sorted elements present in a but not in b.",
        cases=((([1, 2, 3], [2, 3]), [1]),)),

    OpSpec("setkit", "is_disjoint", _s("""
def is_disjoint(a: list, b: list) -> bool:
    return set(a).isdisjoint(set(b))
"""), "Whether a and b share no elements.",
        cases=((([1, 2], [3, 4]), True), (([1], [1]), False))),

    OpSpec("setkit", "powerset_size", _s("""
def powerset_size(items: list) -> int:
    return 2 ** len(set(items))
"""), "Number of subsets of the set of distinct elements (2**n).",
        cases=((([1, 2, 3],), 8), (([1, 1],), 2))),

    OpSpec("setkit", "mode", _s("""
def mode(items: list) -> object:
    if not items:
        raise ValueError("items must be non-empty")
    counts = {}
    for x in items:
        counts[x] = counts.get(x, 0) + 1
    best = max(counts.values())
    return min(k for k, v in counts.items() if v == best)
"""), "Most frequent element; on a tie return the smallest such element.",
        cases=((([1, 2, 2, 3, 3],), 2), (([5],), 5)),
        raises=[([],)]),

    # ===================== codekit — encoding/ciphers =====================
    OpSpec("codekit", "caesar", _s("""
def caesar(s: str, k: int) -> str:
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr((ord(c) - 97 + k) % 26 + 97))
        elif "A" <= c <= "Z":
            out.append(chr((ord(c) - 65 + k) % 26 + 65))
        else:
            out.append(c)
    return "".join(out)
"""), "Caesar-shift the letters of s forward by k (wrapping within case).",
        cases=((("abc", 1), "bcd"), (("XYZ", 3), "ABC"))),

    OpSpec("codekit", "run_length_encode", _s("""
def run_length_encode(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        j = i
        while j < len(s) and s[j] == c:
            j += 1
        out.append(c + str(j - i))
        i = j
    return "".join(out)
"""), 'Run-length encode a string, e.g. "aaabb" -> "a3b2".',
        cases=((("aaabbc",), "a3b2c1"), (("",), ""))),

    OpSpec("codekit", "run_length_decode", _s("""
def run_length_decode(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        j = i + 1
        while j < len(s) and s[j].isdigit():
            j += 1
        count = int(s[i + 1:j])
        out.append(c * count)
        i = j
    return "".join(out)
"""), 'Decode a run-length string, e.g. "a3b2" -> "aaabb".',
        cases=((("a3b2c1",), "aaabbc"),)),

    OpSpec("codekit", "to_binary", _s("""
def to_binary(n: int) -> str:
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return "0"
    out = []
    while n:
        out.append(str(n & 1))
        n >>= 1
    return "".join(reversed(out))
"""), "Binary string for non-negative n (no prefix).",
        cases=(((10,), "1010"), ((0,), "0")),
        raises=[(-1,)]),

    OpSpec("codekit", "from_binary", _s("""
def from_binary(s: str) -> int:
    return int(s, 2)
"""), "Parse a binary digit string into an integer.",
        cases=((("1010",), 10), (("0",), 0))),

    OpSpec("codekit", "xor_encode", _s("""
def xor_encode(s: str, key: int) -> list:
    return [ord(c) ^ key for c in s]
"""), "XOR each character code of s with key, returning the list of codes.",
        cases=((("AB", 1), [64, 67]), (("", 5), []))),

    OpSpec("codekit", "checksum", _s("""
def checksum(s: str) -> int:
    return sum(ord(c) for c in s) % 256
"""), "Sum of character codes of s modulo 256.",
        cases=((("ABC",), 198), (("",), 0))),

    OpSpec("codekit", "hex_encode", _s("""
def hex_encode(s: str) -> str:
    return "".join(format(ord(c), "02x") for c in s)
"""), "Encode each character of s as two lower-case hex digits.",
        cases=((("AB",), "4142"),)),

    OpSpec("codekit", "hex_decode", _s("""
def hex_decode(h: str) -> str:
    return "".join(chr(int(h[i:i + 2], 16)) for i in range(0, len(h), 2))
"""), "Decode a hex-digit string back into characters.",
        cases=((("4142",), "AB"),)),

    OpSpec("codekit", "atbash", _s("""
def atbash(s: str) -> str:
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr(219 - ord(c)))
        elif "A" <= c <= "Z":
            out.append(chr(155 - ord(c)))
        else:
            out.append(c)
    return "".join(out)
"""), "Apply the Atbash cipher (mirror each letter within its alphabet).",
        cases=((("abc",), "zyx"), (("XYZ",), "CBA"))),
]


def modules() -> list:
    """Distinct module names, in first-appearance order."""
    seen: list = []
    for op in OPS:
        if op.module not in seen:
            seen.append(op.module)
    return seen


def expected_tests() -> int:
    """One per-op test + two registry tests (is-registered, dispatches) per op."""
    return 3 * len(OPS)
