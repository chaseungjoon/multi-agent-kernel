"""Specification for ``project_template_3`` — the *real-world contention* target.

Templates ``basic`` and ``2`` are **maximally contended**: every operation funnels
through one shared registry function, which proves MAK's zero-conflict property but
also serializes every task. Real feature work is not shaped like that. This template
models it: a small e-commerce backend (``app``) with

- **8 feature modules** (``accounts``, ``catalog``, ``cart``, ``orders``,
  ``payments``, ``shipping``, ``reviews``, ``search``) — each owned by one agent,
  like real feature ownership; and
- **4 cross-cutting shared tables** — the files real teams collide on:
  ``routes`` (URL dispatch), ``events`` (event handlers), ``errors`` (error-code
  catalog), and ``settings`` (config defaults). Each follows the same in-file
  protocol as the other templates (``register(...)`` lines inside
  ``_register_all``), so the same deterministic union-merge applies.

Each operation implements one function and registers into **zero, one, or two** of
the shared tables. That partial contention is the point: under MAK, tasks touching
different tables (or none) run fully in parallel and only same-table edits briefly
serialize, while a worktree-per-agent run still collides on *every* shared table at
merge time — several conflicted files per merge instead of one.

The on-disk template (stub modules + shared tables + tests) is generated from this
file by ``tools/gen_template3.py``, so stubs, references, and tests cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

PACKAGE = "app"
SHARED_TABLES = ("routes", "events", "errors", "settings")


def _s(text: str) -> str:
    """Normalise a flush-left triple-quoted function source: strip framing newlines."""
    return text.strip("\n") + "\n"


@dataclass(frozen=True)
class Entry:
    """One registration into a shared table: ``register(key, value_expr)``."""

    table: str  # "routes" | "events" | "errors" | "settings"
    key: str
    value: str  # python expression for the registered value

    @property
    def line(self) -> str:
        """The single line this entry adds to the table's ``_register_all``."""
        return f"    register({self.key!r}, {self.value})"


@dataclass(frozen=True)
class Op3Spec:
    """One feature task: implement a function, register into 0-2 shared tables."""

    module: str
    name: str
    source: str  # full reference function source (def header + body)
    doc: str  # one-line docstring placed in the stub the agent implements
    cases: tuple  # ((args_tuple, expected), ...) — at least one
    raises: tuple = ()  # (args_tuple, ...) expected to raise ValueError
    routes: tuple = ()  # route keys; registered value is the op function itself
    events: tuple = ()  # event names; registered value is the op function itself
    errors: tuple = ()  # ((code, message), ...) registered as plain strings
    settings: tuple = ()  # ((key, value), ...) registered as literals

    @property
    def header(self) -> str:
        """The ``def ...:`` line, taken from the reference source."""
        return self.source.splitlines()[0]

    @property
    def entries(self) -> tuple[Entry, ...]:
        """Every shared-table registration this operation performs."""
        out: list[Entry] = []
        for key in self.routes:
            out.append(Entry("routes", key, f"{self.module}.{self.name}"))
        for key in self.events:
            out.append(Entry("events", key, f"{self.module}.{self.name}"))
        for code, message in self.errors:
            out.append(Entry("errors", code, repr(message)))
        for key, value in self.settings:
            out.append(Entry("settings", key, repr(value)))
        return tuple(out)


OPS: list[Op3Spec] = [
    # ===================== accounts =====================
    Op3Spec("accounts", "validate_username", _s("""
def validate_username(u: str) -> bool:
    if len(u) < 3 or len(u) > 20:
        return False
    if not u[0].isalpha() or not u[0].islower():
        return False
    return all(c.islower() or c.isdigit() or c == "_" for c in u)
"""), "True iff u is 3-20 chars, starts with a lowercase letter, and contains only "
        "lowercase letters, digits, or underscores.",
        cases=((("alice_1",), True), (("1abc",), False), (("ab",), False),
               (("Alice",), False)),
        routes=("POST /accounts/validate",)),

    Op3Spec("accounts", "password_strength", _s("""
def password_strength(p: str) -> str:
    score = 0
    if len(p) >= 8:
        score += 1
    if any(c.isupper() for c in p):
        score += 1
    if any(c.isdigit() for c in p):
        score += 1
    if any(not c.isalnum() for c in p):
        score += 1
    if score <= 1:
        return "weak"
    if score <= 3:
        return "medium"
    return "strong"
"""), 'Score p one point each for len>=8, an uppercase letter, a digit, and a '
        'non-alphanumeric char; return "weak" (score<=1), "medium" (2-3), or '
        '"strong" (4).',
        cases=((("abc",), "weak"), (("Password1",), "medium"),
               (("Password1!",), "strong")),
        settings=(("security.min_password_length", 8),)),

    Op3Spec("accounts", "mask_email", _s("""
def mask_email(e: str) -> str:
    if "@" not in e:
        raise ValueError("not an email address")
    local, _, domain = e.partition("@")
    if not local:
        raise ValueError("empty local part")
    return local[0] + "***" + local[-1] + "@" + domain
"""), 'Mask the local part of an email as first char + "***" + last char (e.g. '
        '"alice@shop.com" -> "a***e@shop.com"); ValueError if there is no "@" or '
        "the local part is empty.",
        cases=((("alice@shop.com",), "a***e@shop.com"), (("bo@x.io",), "b***o@x.io")),
        raises=[("not-an-email",), ("@x.com",)],
        errors=((("accounts.invalid_email"), "Email address is not valid"),)),

    Op3Spec("accounts", "initials", _s("""
def initials(full_name: str) -> str:
    parts = full_name.split()
    if not parts:
        raise ValueError("empty name")
    return "".join(p[0].upper() + "." for p in parts)
"""), 'Return the dotted uppercase initials of a full name, e.g. '
        '"john ronald tolkien" -> "J.R.T."; ValueError on an empty/whitespace name.',
        cases=((("john ronald tolkien",), "J.R.T."), (("ada",), "A.")),
        raises=[("   ",)]),

    Op3Spec("accounts", "signup_greeting", _s("""
def signup_greeting(name: str) -> str:
    return f"Welcome, {name.title()}!"
"""), 'Return "Welcome, <Name Title-Cased>!" for the new account, e.g. '
        '"mary anne" -> "Welcome, Mary Anne!".',
        cases=((("ada",), "Welcome, Ada!"), (("mary anne",), "Welcome, Mary Anne!")),
        events=("account.created",)),

    Op3Spec("accounts", "is_adult", _s("""
def is_adult(birth_year: int, current_year: int) -> bool:
    if birth_year > current_year:
        raise ValueError("birth year is in the future")
    return current_year - birth_year >= 18
"""), "True iff current_year - birth_year >= 18; ValueError if birth_year is after "
        "current_year.",
        cases=(((2000, 2026), True), ((2010, 2026), False), ((2008, 2026), True)),
        raises=[(2030, 2026)],
        settings=(("accounts.min_age", 18),)),

    Op3Spec("accounts", "login_throttle_delay", _s("""
def login_throttle_delay(attempts: int) -> int:
    if attempts < 0:
        raise ValueError("attempts must be non-negative")
    if attempts < 3:
        return 0
    return min(60, 2 ** (attempts - 3))
"""), "Seconds to wait before the next sign-in attempt: 0 for fewer than 3 failed "
        "attempts, else 2**(attempts-3) capped at 60; ValueError on negative attempts.",
        cases=(((2,), 0), ((3,), 1), ((5,), 4), ((10,), 60)),
        raises=[(-1,)],
        errors=((("accounts.locked"), "Too many failed sign-in attempts"),)),

    # ===================== catalog =====================
    Op3Spec("catalog", "slugify", _s("""
def slugify(title: str) -> str:
    out = []
    for c in title.lower():
        out.append(c if c.isalnum() else "-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")
"""), "Lowercase the title, replace every non-alphanumeric char with a hyphen, "
        "collapse runs of hyphens, and strip leading/trailing hyphens.",
        cases=((("Hello, World!",), "hello-world"),
               (("  Django 5 -- Release  ",), "django-5-release")),
        routes=("GET /catalog/slug",)),

    Op3Spec("catalog", "format_price", _s("""
def format_price(cents: int) -> str:
    if cents < 0:
        raise ValueError("cents must be non-negative")
    return f"${cents // 100}.{cents % 100:02d}"
"""), 'Format integer cents as "$D.DD", e.g. 1234 -> "$12.34" and 5 -> "$0.05"; '
        "ValueError on negative cents.",
        cases=(((1234,), "$12.34"), ((5,), "$0.05"), ((0,), "$0.00")),
        raises=[(-1,)],
        routes=("GET /catalog/price",)),

    Op3Spec("catalog", "apply_discount", _s("""
def apply_discount(cents: int, percent: int) -> int:
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return cents * (100 - percent) // 100
"""), "Return the price after the discount, floored to whole cents: "
        "cents*(100-percent)//100; ValueError unless 0 <= percent <= 100.",
        cases=(((1000, 25), 750), ((999, 10), 899), ((500, 0), 500)),
        raises=[(100, 101), (100, -1)],
        errors=((("catalog.bad_discount"),
                 "Discount must be between 0 and 100 percent"),)),

    Op3Spec("catalog", "in_stock", _s("""
def in_stock(stock: int, qty: int) -> bool:
    if qty <= 0:
        raise ValueError("qty must be positive")
    return stock >= qty
"""), "True iff stock covers the requested qty; ValueError if qty <= 0.",
        cases=(((5, 3), True), ((2, 3), False)),
        raises=[(5, 0)]),

    Op3Spec("catalog", "sku", _s("""
def sku(category: str, number: int) -> str:
    if not category:
        raise ValueError("empty category")
    if number <= 0:
        raise ValueError("number must be positive")
    return f"{category[:3].upper()}-{number:05d}"
"""), 'Build a SKU from the first three characters of category uppercased, a '
        'hyphen, and the number zero-padded to five digits, e.g. ("shoes", 42) -> '
        '"SHO-00042"; ValueError on an empty category or non-positive number.',
        cases=((("shoes", 42), "SHO-00042"), (("tv", 7), "TV-00007")),
        raises=[("", 1), ("x", 0)],
        settings=(("catalog.sku_width", 5),)),

    Op3Spec("catalog", "star_bar", _s("""
def star_bar(n: int) -> str:
    if n < 0 or n > 5:
        raise ValueError("n must be between 0 and 5")
    return "\\u2605" * n + "\\u2606" * (5 - n)
"""), "Render an n-of-5 star bar using U+2605 (filled) then U+2606 (empty), e.g. "
        '3 -> "★★★☆☆"; ValueError unless 0 <= n <= 5.',
        cases=(((3,), "★★★☆☆"),
               ((0,), "☆☆☆☆☆")),
        raises=[(6,), (-1,)]),

    Op3Spec("catalog", "list_page", _s("""
def list_page(items: list, page: int, size: int) -> list:
    if page < 1:
        raise ValueError("page must be >= 1")
    if size < 1:
        raise ValueError("size must be >= 1")
    start = (page - 1) * size
    return items[start:start + size]
"""), "Return the 1-based page of items of the given size (may be empty past the "
        "end); ValueError if page < 1 or size < 1.",
        cases=((([10, 20, 30, 40, 50], 2, 2), [30, 40]),
               (([1, 2, 3], 1, 5), [1, 2, 3])),
        raises=[([1], 0, 2), ([1], 1, 0)],
        routes=("GET /catalog/page",)),

    Op3Spec("catalog", "bulk_price", _s("""
def bulk_price(cents: int, qty: int) -> int:
    if qty <= 0:
        raise ValueError("qty must be positive")
    if qty >= 50:
        return cents * 80 // 100
    if qty >= 10:
        return cents * 90 // 100
    return cents
"""), "Per-unit price after volume discount, floored: 20% off for qty >= 50, 10% "
        "off for qty >= 10, else unchanged; ValueError if qty <= 0.",
        cases=(((1000, 5), 1000), ((1000, 10), 900), ((1000, 50), 800)),
        raises=[(1000, 0)],
        settings=(("catalog.bulk_threshold", 10),)),

    # ===================== cart =====================
    Op3Spec("cart", "cart_total", _s("""
def cart_total(prices: list) -> int:
    if any(p < 0 for p in prices):
        raise ValueError("negative price")
    return sum(prices)
"""), "Sum a list of integer cent prices (empty list -> 0); ValueError if any "
        "price is negative.",
        cases=((([100, 250],), 350), (([],), 0)),
        raises=[([100, -5],)],
        routes=("GET /cart/total",)),

    Op3Spec("cart", "add_item", _s("""
def add_item(items: list, item: str, max_items: int) -> list:
    if len(items) >= max_items:
        raise ValueError("cart is full")
    return items + [item]
"""), "Return a NEW list with item appended; ValueError when the cart already "
        "holds max_items entries.",
        cases=(((["a"], "b", 3), ["a", "b"]), (([], "x", 1), ["x"])),
        raises=[(["a", "b"], "c", 2)],
        routes=("POST /cart/items",),
        errors=((("cart.limit_exceeded"),
                 "Cart cannot exceed the maximum number of items"),)),

    Op3Spec("cart", "item_counts", _s("""
def item_counts(items: list) -> dict:
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts
"""), "Count occurrences of each item, e.g. ['a','b','a'] -> {'a': 2, 'b': 1}.",
        cases=(((["a", "b", "a"],), {"a": 2, "b": 1}), (([],), {}))),

    Op3Spec("cart", "apply_coupon", _s("""
def apply_coupon(total: int, code: str) -> int:
    if code == "SAVE10":
        return total * 90 // 100
    if code == "SAVE25":
        return total * 75 // 100
    raise ValueError("unknown coupon")
"""), 'Apply coupon "SAVE10" (10% off) or "SAVE25" (25% off) to the total, '
        "floored to whole cents; ValueError for any other code.",
        cases=(((1000, "SAVE10"), 900), ((1000, "SAVE25"), 750)),
        raises=[(1000, "HELLO")],
        errors=((("cart.bad_coupon"), "Coupon code is not recognized"),)),

    Op3Spec("cart", "free_shipping_eligible", _s("""
def free_shipping_eligible(total_cents: int, threshold: int) -> bool:
    return total_cents >= threshold
"""), "True iff the cart total meets or exceeds the free-shipping threshold.",
        cases=(((5000, 5000), True), ((4999, 5000), False)),
        settings=(("cart.free_shipping_cents", 5000),)),

    Op3Spec("cart", "remove_item", _s("""
def remove_item(items: list, item: str) -> list:
    if item not in items:
        raise ValueError("item not in cart")
    out = list(items)
    out.remove(item)
    return out
"""), "Return a NEW list with the FIRST occurrence of item removed; ValueError if "
        "the item is not present.",
        cases=(((["a", "b", "a"], "a"), ["b", "a"]),),
        raises=[(["b"], "x")]),

    Op3Spec("cart", "quantity_update", _s("""
def quantity_update(counts: dict, item: str, qty: int) -> dict:
    if qty < 0:
        raise ValueError("qty must be non-negative")
    out = dict(counts)
    if qty == 0:
        out.pop(item, None)
    else:
        out[item] = qty
    return out
"""), "Return a NEW dict with item's quantity set to qty (qty of 0 removes the "
        "item); ValueError on negative qty.",
        cases=((({"a": 1}, "b", 3), {"a": 1, "b": 3}),
               (({"a": 2, "b": 1}, "a", 0), {"b": 1})),
        raises=[({}, "a", -1)],
        routes=("POST /cart/quantity",)),

    # ===================== orders =====================
    Op3Spec("orders", "order_number", _s("""
def order_number(seq: int, year: int) -> str:
    if seq <= 0:
        raise ValueError("seq must be positive")
    return f"ORD-{year}-{seq:06d}"
"""), 'Build an order number "ORD-<year>-<seq zero-padded to 6>", e.g. (42, 2026) '
        '-> "ORD-2026-000042"; ValueError if seq <= 0.',
        cases=(((42, 2026), "ORD-2026-000042"),),
        raises=[(0, 2026)],
        routes=("POST /orders",)),

    Op3Spec("orders", "next_status", _s("""
def next_status(status: str) -> str:
    chain = {"placed": "paid", "paid": "shipped", "shipped": "delivered"}
    if status not in chain:
        raise ValueError("no next status")
    return chain[status]
"""), 'Advance an order status along placed -> paid -> shipped -> delivered; '
        'ValueError for "delivered" or any unknown status.',
        cases=((("placed",), "paid"), (("shipped",), "delivered")),
        raises=[("delivered",), ("weird",)],
        events=("order.status_changed",)),

    Op3Spec("orders", "order_total", _s("""
def order_total(subtotal: int, tax_bp: int, ship: int) -> int:
    if subtotal < 0 or tax_bp < 0 or ship < 0:
        raise ValueError("negative amount")
    return subtotal + subtotal * tax_bp // 10000 + ship
"""), "Total = subtotal + floor(subtotal*tax_bp/10000) + ship, where tax_bp is "
        "basis points (875 = 8.75%); ValueError on any negative input.",
        cases=(((10000, 875, 500), 11375), ((0, 875, 0), 0)),
        raises=[(10000, 875, -1)],
        routes=("GET /orders/total",),
        settings=(("orders.tax_basis_points", 875),)),

    Op3Spec("orders", "confirmation_line", _s("""
def confirmation_line(order_no: str, total_cents: int) -> str:
    dollars = f"${total_cents // 100}.{total_cents % 100:02d}"
    return f"Order {order_no} confirmed - total {dollars}"
"""), 'Return "Order <no> confirmed - total $D.DD", e.g. ("ORD-2026-000042", '
        '11375) -> "Order ORD-2026-000042 confirmed - total $113.75".',
        cases=((("ORD-2026-000042", 11375),
                "Order ORD-2026-000042 confirmed - total $113.75"),),
        events=("order.placed",)),

    Op3Spec("orders", "estimated_delivery", _s("""
def estimated_delivery(day_of_week: int, transit_days: int) -> int:
    if day_of_week < 0 or day_of_week > 6:
        raise ValueError("day_of_week must be 0-6")
    if transit_days < 0:
        raise ValueError("transit_days must be non-negative")
    return (day_of_week + transit_days) % 7
"""), "Delivery day of week: (day_of_week + transit_days) % 7 with days numbered "
        "0-6; ValueError if day_of_week is outside 0-6 or transit_days is negative.",
        cases=(((5, 3), 1), ((0, 7), 0)),
        raises=[(7, 1), (-1, 1)]),

    Op3Spec("orders", "cancel_allowed", _s("""
def cancel_allowed(status: str) -> bool:
    if status not in ("placed", "paid", "shipped", "delivered"):
        raise ValueError("unknown status")
    return status in ("placed", "paid")
"""), 'True iff an order in this status may still be cancelled ("placed" or '
        '"paid"); ValueError for a status outside placed/paid/shipped/delivered.',
        cases=((("placed",), True), (("shipped",), False)),
        raises=[("weird",)],
        errors=((("orders.cancel_denied"), "Order can no longer be cancelled"),)),

    Op3Spec("orders", "split_shipments", _s("""
def split_shipments(items: list, box_size: int) -> list:
    if box_size <= 0:
        raise ValueError("box_size must be positive")
    return [items[i:i + box_size] for i in range(0, len(items), box_size)]
"""), "Chunk items into boxes of at most box_size, preserving order, e.g. "
        "([1,2,3,4,5], 2) -> [[1,2],[3,4],[5]]; ValueError if box_size <= 0.",
        cases=((([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]), (([], 3), [])),
        raises=[([1], 0)]),

    Op3Spec("orders", "refund_amount", _s("""
def refund_amount(total: int, days: int, window: int) -> int:
    if window <= 0:
        raise ValueError("window must be positive")
    if days <= window:
        return total
    if days <= 2 * window:
        return total // 2
    return 0
"""), "Refund policy: full refund within `window` days of purchase, half (floored) "
        "within twice the window, else 0; ValueError if window <= 0.",
        cases=(((1000, 10, 30), 1000), ((1000, 45, 30), 500), ((999, 45, 30), 499),
               ((1000, 61, 30), 0)),
        raises=[(1000, 1, 0)],
        settings=(("orders.refund_window_days", 30),)),

    # ===================== payments =====================
    Op3Spec("payments", "luhn_valid", _s("""
def luhn_valid(number: str) -> bool:
    if not number or not number.isdigit():
        raise ValueError("number must be a non-empty digit string")
    total = 0
    for i, c in enumerate(reversed(number)):
        d = int(c)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
"""), "Luhn checksum over a digit string: double every second digit from the "
        "right (subtracting 9 when > 9) and require the sum % 10 == 0; ValueError "
        "on an empty or non-digit input.",
        cases=((("79927398713",), True), (("79927398710",), False)),
        raises=[("79a",), ("",)],
        routes=("POST /payments/validate",)),

    Op3Spec("payments", "mask_card", _s("""
def mask_card(number: str) -> str:
    if len(number) < 4:
        raise ValueError("card number too short")
    return "**** **** **** " + number[-4:]
"""), 'Mask a card number keeping only the last four digits: '
        '"**** **** **** 1234"; ValueError if fewer than 4 characters.',
        cases=((("4111111111111111",), "**** **** **** 1111"),
               (("12345",), "**** **** **** 2345")),
        raises=[("123",)]),

    Op3Spec("payments", "processing_fee", _s("""
def processing_fee(cents: int, bp: int) -> int:
    if cents < 0 or bp < 0:
        raise ValueError("negative amount")
    return cents * bp // 10000
"""), "Fee = floor(cents * bp / 10000) where bp is basis points (290 = 2.90%); "
        "ValueError on any negative input.",
        cases=(((10000, 290), 290), ((999, 290), 28)),
        raises=[(-1, 290)],
        settings=(("payments.fee_basis_points", 290),)),

    Op3Spec("payments", "split_evenly", _s("""
def split_evenly(total: int, n: int) -> list:
    if n <= 0:
        raise ValueError("n must be positive")
    base = total // n
    rem = total % n
    return [base + 1] * rem + [base] * (n - rem)
"""), "Split total cents across n payers: the first (total % n) payers pay one "
        "cent more, e.g. (10, 3) -> [4, 3, 3]; ValueError if n <= 0.",
        cases=(((10, 3), [4, 3, 3]), ((9, 3), [3, 3, 3])),
        raises=[(5, 0)],
        routes=("GET /payments/split",)),

    Op3Spec("payments", "currency_to_cents", _s("""
def currency_to_cents(s: str) -> int:
    if not s.startswith("$"):
        raise ValueError("amount must start with $")
    dollars, sep, cents = s[1:].partition(".")
    if not sep or len(cents) != 2 or not dollars.isdigit() or not cents.isdigit():
        raise ValueError("amount must look like $D.DD")
    return int(dollars) * 100 + int(cents)
"""), 'Parse "$D.DD" into integer cents, e.g. "$12.34" -> 1234; ValueError unless '
        "the string is a $, digits, a dot, and exactly two decimal digits.",
        cases=((("$12.34",), 1234), (("$0.05",), 5)),
        raises=[("12.34",), ("$12.3",), ("$12",)],
        routes=("POST /payments/parse",),
        errors=((("payments.bad_amount"), "Amount must look like $D.DD"),)),

    Op3Spec("payments", "is_expired", _s("""
def is_expired(exp_month: int, exp_year: int, now_month: int, now_year: int) -> bool:
    for month in (exp_month, now_month):
        if month < 1 or month > 12:
            raise ValueError("month must be 1-12")
    return (exp_year, exp_month) < (now_year, now_month)
"""), "True iff the card (valid through the END of its expiry month) is expired "
        "at now_month/now_year; ValueError for any month outside 1-12.",
        cases=(((6, 2026, 7, 2026), True), ((7, 2026, 7, 2026), False),
               ((1, 2027, 12, 2026), False)),
        raises=[(13, 2026, 1, 2026)]),

    Op3Spec("payments", "receipt_id", _s("""
def receipt_id(order_no: str, attempt: int) -> str:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return f"{order_no}/R{attempt:02d}"
"""), 'Build a receipt id "<order_no>/RNN" with the attempt zero-padded to two '
        'digits, e.g. ("ORD-2026-000042", 3) -> "ORD-2026-000042/R03"; ValueError '
        "if attempt < 1.",
        cases=((("ORD-2026-000042", 3), "ORD-2026-000042/R03"),),
        raises=[("X", 0)],
        events=("payment.captured",)),

    # ===================== shipping =====================
    Op3Spec("shipping", "shipping_cost", _s("""
def shipping_cost(weight_g: int, zone: int) -> int:
    bases = {1: 500, 2: 900, 3: 1400}
    if zone not in bases:
        raise ValueError("unknown zone")
    if weight_g <= 0:
        raise ValueError("weight must be positive")
    started_kg = (weight_g + 999) // 1000
    return bases[zone] + started_kg * 100
"""), "Cost in cents: zone base (1->500, 2->900, 3->1400) plus 100 per *started* "
        "kilogram; ValueError for an unknown zone or non-positive weight.",
        cases=(((1500, 1), 700), ((1000, 2), 1000), ((1, 3), 1500)),
        raises=[(0, 1), (100, 9)],
        routes=("GET /shipping/cost",)),

    Op3Spec("shipping", "normalize_postcode", _s("""
def normalize_postcode(s: str) -> str:
    code = s.replace(" ", "").upper()
    if not code or not code.isalnum():
        raise ValueError("postcode must be alphanumeric")
    return code
"""), 'Remove all spaces and uppercase the postcode, e.g. " se1 9gf " -> '
        '"SE19GF"; ValueError if the result is empty or not alphanumeric.',
        cases=(((" se1 9gf ",), "SE19GF"), (("10001",), "10001")),
        raises=[("",), ("s!1",)]),

    Op3Spec("shipping", "address_label", _s("""
def address_label(name: str, street: str, city: str, code: str) -> str:
    if not name:
        raise ValueError("empty name")
    return f"{name.upper()}\\n{street}\\n{city} {code}"
"""), 'Three-line label: NAME uppercased, then street, then "city code" joined by '
        "a space; ValueError on an empty name.",
        cases=((("Ada Lovelace", "12 Byte St", "London", "SE1"),
                "ADA LOVELACE\n12 Byte St\nLondon SE1"),),
        raises=[("", "s", "c", "z")],
        routes=("POST /shipping/label",)),

    Op3Spec("shipping", "delivery_window", _s("""
def delivery_window(days: int, express: bool) -> int:
    if days < 1:
        raise ValueError("days must be >= 1")
    if express:
        return max(1, days // 2)
    return days
"""), "Estimated delivery days: express halves the standard estimate (floored, "
        "minimum 1), otherwise unchanged; ValueError if days < 1.",
        cases=(((6, True), 3), ((1, True), 1), ((5, False), 5)),
        raises=[(0, False)],
        settings=(("shipping.express_divisor", 2),)),

    Op3Spec("shipping", "tracking_valid", _s("""
def tracking_valid(code: str) -> bool:
    if not code:
        raise ValueError("empty tracking code")
    return (
        len(code) == 11
        and code[:2].isalpha()
        and code[:2].isupper()
        and code[2:].isdigit()
    )
"""), "True iff the tracking code is exactly two uppercase letters followed by "
        "nine digits (11 chars total); ValueError on an empty string.",
        cases=((("AB123456789",), True), (("ab123456789",), False),
               (("AB12345678",), False)),
        raises=[("",)]),

    Op3Spec("shipping", "zone_for_country", _s("""
def zone_for_country(country: str) -> int:
    if not country:
        raise ValueError("empty country")
    zones = {"us": 1, "ca": 1, "uk": 2, "de": 2, "fr": 2}
    return zones.get(country.lower(), 3)
"""), "Shipping zone by country code (case-insensitive): us/ca -> 1, uk/de/fr -> "
        "2, anything else -> 3; ValueError on an empty string.",
        cases=((("US",), 1), (("uk",), 2), (("jp",), 3)),
        raises=[("",)],
        settings=(("shipping.default_zone", 3),)),

    Op3Spec("shipping", "oversize_surcharge", _s("""
def oversize_surcharge(weight_g: int, limit_g: int, per_kg_cents: int) -> int:
    if limit_g <= 0:
        raise ValueError("limit must be positive")
    if weight_g <= limit_g:
        return 0
    over_kg = (weight_g - limit_g + 999) // 1000
    return over_kg * per_kg_cents
"""), "Surcharge for weight over the limit: per_kg_cents for every *started* kg "
        "over (0 when within the limit); ValueError if limit_g <= 0.",
        cases=(((25000, 20000, 150), 750), ((20000, 20000, 150), 0),
               ((20001, 20000, 150), 150)),
        raises=[(100, 0, 10)],
        errors=((("shipping.oversize"),
                 "Package exceeds the standard weight limit"),)),

    # ===================== reviews =====================
    Op3Spec("reviews", "average_rating", _s("""
def average_rating(ratings: list) -> float:
    if not ratings:
        raise ValueError("no ratings")
    if any(r < 1 or r > 5 for r in ratings):
        raise ValueError("ratings must be 1-5")
    avg = sum(ratings) / len(ratings)
    return int(avg * 10 + 0.5) / 10
"""), "Mean rating rounded HALF UP to one decimal (e.g. [3,4,4] -> 3.7); "
        "ValueError on an empty list or any rating outside 1-5.",
        cases=((([4, 5],), 4.5), (([3, 4, 4],), 3.7), (([5],), 5.0)),
        raises=[([],), ([0],), ([6],)],
        routes=("GET /reviews/average",)),

    Op3Spec("reviews", "star_histogram", _s("""
def star_histogram(ratings: list) -> dict:
    if any(r < 1 or r > 5 for r in ratings):
        raise ValueError("ratings must be 1-5")
    hist = {star: 0 for star in (1, 2, 3, 4, 5)}
    for r in ratings:
        hist[r] += 1
    return hist
"""), "Histogram of ratings with ALL keys 1-5 present (zero when absent); "
        "ValueError for any rating outside 1-5.",
        cases=((([5, 5, 3],), {1: 0, 2: 0, 3: 1, 4: 0, 5: 2}),),
        raises=[([0],)]),

    Op3Spec("reviews", "contains_profanity", _s("""
def contains_profanity(text: str, banned: list) -> bool:
    tokens = []
    cur = ""
    for c in text.lower():
        if c.isalnum():
            cur += c
        elif cur:
            tokens.append(cur)
            cur = ""
    if cur:
        tokens.append(cur)
    return any(word.lower() in tokens for word in banned)
"""), "True iff any banned word matches a WHOLE word of text, case-insensitively "
        "(substrings inside longer words do not count).",
        cases=((("This is Darn good", ["darn"]), True),
               (("clean text", ["darn"]), False),
               (("scandarnous", ["darn"]), False)),
        errors=((("reviews.rejected"), "Review contains prohibited language"),)),

    Op3Spec("reviews", "helpfulness", _s("""
def helpfulness(up: int, down: int) -> int:
    if up < 0 or down < 0:
        raise ValueError("votes must be non-negative")
    if up + down == 0:
        return 0
    return up * 100 // (up + down)
"""), "Helpfulness percentage floored: up*100 // (up+down), and 0 when there are "
        "no votes; ValueError on negative votes.",
        cases=(((3, 1), 75), ((0, 0), 0), ((1, 2), 33)),
        raises=[(-1, 0)]),

    Op3Spec("reviews", "truncate_review", _s("""
def truncate_review(text: str, n: int) -> str:
    if n < 1:
        raise ValueError("n must be >= 1")
    if len(text) <= n:
        return text
    return text[: n - 1] + "\\u2026"
"""), "Truncate text to at most n chars, replacing the tail with a single "
        "ellipsis character (U+2026) when shortened; ValueError if n < 1.",
        cases=((("great product would buy again", 12), "great produ…"),
               (("nice", 10), "nice")),
        raises=[("x", 0)],
        routes=("GET /reviews/preview",)),

    Op3Spec("reviews", "verified_badge", _s("""
def verified_badge(purchased: bool, rating: int) -> str:
    if rating < 1 or rating > 5:
        raise ValueError("rating must be 1-5")
    star = f"\\u2605{rating}"
    return f"Verified {star}" if purchased else star
"""), 'Return "Verified ★N" for a verified purchase else "★N" (N is the '
        "1-5 rating); ValueError for a rating outside 1-5.",
        cases=(((True, 4), "Verified ★4"), ((False, 5), "★5")),
        raises=[(True, 0), (False, 6)],
        events=("review.submitted",)),

    Op3Spec("reviews", "sort_reviews", _s("""
def sort_reviews(pairs: list) -> list:
    return sorted(pairs, key=lambda p: (-p[0], p[1]))
"""), "Sort (helpfulness, review_id) pairs by helpfulness descending, then id "
        "ascending.",
        cases=((([(10, "b"), (50, "a"), (10, "a")],),
                [(50, "a"), (10, "a"), (10, "b")]),)),

    # ===================== search =====================
    Op3Spec("search", "tokenize", _s("""
def tokenize(q: str) -> list:
    tokens = []
    cur = ""
    for c in q.lower():
        if c.isalnum():
            cur += c
        elif cur:
            tokens.append(cur)
            cur = ""
    if cur:
        tokens.append(cur)
    return tokens
"""), "Lowercase the query and split it into alphanumeric tokens (punctuation "
        "and spaces separate tokens; empties dropped).",
        cases=((("Hello, World HELLO",), ["hello", "world", "hello"]),
               (("",), [])),
        routes=("GET /search",)),

    Op3Spec("search", "match_score", _s("""
def match_score(query_tokens: list, doc_tokens: list) -> int:
    return len(set(query_tokens) & set(doc_tokens))
"""), "Number of DISTINCT query tokens that appear in the document tokens.",
        cases=(((["red", "shoe"], ["shoe", "red", "laces"]), 2),
               ((["red"], ["blue"]), 0))),

    Op3Spec("search", "highlight", _s("""
def highlight(text: str, term: str) -> str:
    if not term:
        raise ValueError("empty term")
    out = []
    low_text, low_term = text.lower(), term.lower()
    i = 0
    while i < len(text):
        j = low_text.find(low_term, i)
        if j == -1:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append("[" + text[j:j + len(term)] + "]")
        i = j + len(term)
    return "".join(out)
"""), "Wrap every case-insensitive occurrence of term in square brackets, "
        'preserving the original casing, e.g. ("The Cat sat on a cat", "cat") -> '
        '"The [Cat] sat on a [cat]"; ValueError on an empty term.',
        cases=((("The Cat sat on a cat", "cat"), "The [Cat] sat on a [cat]"),
               (("no match here", "zz"), "no match here")),
        raises=[("text", "")],
        routes=("GET /search/highlight",)),

    Op3Spec("search", "suggest", _s("""
def suggest(prefix: str, vocab: list) -> list:
    matches = sorted(w for w in vocab if w.startswith(prefix))
    return matches[:5]
"""), "The first five vocabulary words that start with the prefix, sorted "
        "alphabetically.",
        cases=((("ca", ["cart", "cat", "castle", "carbon", "cave", "cap"]),
                ["cap", "carbon", "cart", "castle", "cat"]),
               (("zz", ["cart"]), [])),
        settings=(("search.max_suggestions", 5),)),

    Op3Spec("search", "page_count", _s("""
def page_count(total: int, per_page: int) -> int:
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total < 0:
        raise ValueError("total must be non-negative")
    return (total + per_page - 1) // per_page
"""), "Number of pages needed for `total` results at `per_page` per page "
        "(ceiling; 0 results -> 0 pages); ValueError if per_page <= 0 or total < 0.",
        cases=(((45, 10), 5), ((40, 10), 4), ((0, 10), 0)),
        raises=[(10, 0)]),

    Op3Spec("search", "filter_by_price", _s("""
def filter_by_price(items: list, lo: int, hi: int) -> list:
    if lo > hi:
        raise ValueError("lo must not exceed hi")
    return [(name, price) for name, price in items if lo <= price <= hi]
"""), "Keep (name, price) pairs whose price is within [lo, hi] inclusive, "
        "preserving order; ValueError if lo > hi.",
        cases=((([("a", 5), ("b", 15), ("c", 10)], 5, 10), [("a", 5), ("c", 10)]),),
        raises=[([], 10, 5)],
        routes=("GET /search/filter",)),

    Op3Spec("search", "query_time_bucket", _s("""
def query_time_bucket(ms: int) -> str:
    if ms < 0:
        raise ValueError("ms must be non-negative")
    if ms < 100:
        return "fast"
    if ms < 500:
        return "ok"
    return "slow"
"""), 'Bucket a query latency: < 100ms -> "fast", 100-499ms -> "ok", >= 500ms -> '
        '"slow"; ValueError on a negative latency.',
        cases=(((50,), "fast"), ((100,), "ok"), ((999,), "slow")),
        raises=[(-1,)],
        events=("search.performed",)),
]


def modules() -> list:
    """Distinct feature module names, in first-appearance order."""
    seen: list = []
    for op in OPS:
        if op.module not in seen:
            seen.append(op.module)
    return seen


def table_entries() -> dict:
    """table name -> list of (op, entry) pairs, in spec order."""
    out: dict = {table: [] for table in SHARED_TABLES}
    for op in OPS:
        for entry in op.entries:
            out[entry.table].append((op, entry))
    return out


def expected_tests() -> int:
    """One per-op test + two table tests (is-registered, dispatches) per entry."""
    total_entries = sum(len(op.entries) for op in OPS)
    return len(OPS) + 2 * total_entries
