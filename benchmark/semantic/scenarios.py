"""The nine semantic-conflict shapes of TASKS.

md Wave 20, as runnable scenarios.

Every scenario obeys one contract, checked by ``evaluate.validate``: its oracle
passes on the base project, with edit A alone, and with edit B alone — and
fails on the naive combination (both edits merged with no coordination, B's
side first). Shape 10 (out-of-store artifacts: config, SQL, docs) has no
scenario: those files are not nodes, so there is nothing for either system to
lock or check, and the table says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from mak.core.types import TaskBundle
from semantic.scripted import Response


@dataclass(frozen=True)
class Edit:
    """One scripted agent task: what it targets and what it returns."""

    task_id: str
    description: str
    targets: tuple[str, ...]
    first: Mapping[str, Response]
    # What a competent agent returns when sent back (after reading the retry
    # note / its refreshed bundle). Defaults to repeating ``first``.
    retry: Mapping[str, Response] = field(default_factory=dict)
    context: tuple[str, ...] = ()
    declarations: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    """A project, two edits, and an oracle that fails only on A+B."""

    shape: int
    name: str
    summary: str
    files: Mapping[str, str]
    a: Edit
    b: Edit
    oracle: str  # defines ``check()``; raising means the combined code is wrong
    # Gates MAK needs to detect this shape ("impact_tests"); empty = defaults.
    needs: tuple[str, ...] = ()


def _append(node: str, line: str) -> Response:
    """Append ``line`` to the registrar function the agent was shown."""

    def build(bundle: TaskBundle) -> str:
        current = bundle.context.get(f"write_source:{node}", "")
        body = current.replace("    raise NotImplementedError\n", "")
        if not body.endswith("\n"):
            body += "\n"
        return body + f"    {line}\n"

    return build


def _insert_before(node: str, line: str, anchor: str) -> Response:
    """Insert ``line`` before ``anchor`` if the chain has it, else append."""

    def build(bundle: TaskBundle) -> str:
        current = bundle.context.get(f"write_source:{node}", "")
        if f"    {anchor}\n" in current:
            return current.replace(f"    {anchor}\n", f"    {line}\n    {anchor}\n", 1)
        return current + f"    {line}\n"

    return build


# -- shape 1: stale read ----------------------------------------------------

_S1 = Scenario(
    shape=1,
    name="stale read (write skew)",
    summary=(
        "B is shown load_user (a same-file sibling) and builds on its dict "
        "return; A changes it to return a User while B is in flight."
    ),
    files={
        "shop/__init__.py": "",
        "shop/models.py": (
            "class User:\n"
            "    def __init__(self, uid: int, name: str) -> None:\n"
            "        self.uid = uid\n"
            "        self.name = name\n"
        ),
        "shop/users.py": (
            "from shop.models import User\n\n\n"
            "def load_user(uid):\n"
            "    return {'id': uid, 'name': 'ann'}\n\n\n"
            "def greet(uid):\n"
            "    return 'hi'\n"
        ),
    },
    a=Edit(
        "a", "Return a User object from load_user",
        ("shop/users.py::function::load_user",),
        {"shop/users.py::function::load_user":
         "def load_user(uid) -> User:\n    return User(uid, 'ann')\n"},
    ),
    b=Edit(
        "b", "Greet the user by name",
        ("shop/users.py::function::greet",),
        {"shop/users.py::function::greet":
         "def greet(uid):\n    return 'hi ' + load_user(uid)['name']\n"},
        retry={"shop/users.py::function::greet":
               "def greet(uid):\n    return 'hi ' + load_user(uid).name\n"},
    ),
    oracle=(
        "def check():\n"
        "    from shop.users import greet\n"
        "    assert greet(1) in ('hi', 'hi ann')\n"
    ),
)

# -- shape 2: signature change against a new call --------------------------

_S2 = Scenario(
    shape=2,
    name="signature change vs new call",
    summary=(
        "A adds a required parameter to send(); B, in another file, adds a new "
        "call send('report') that plan validation cannot see."
    ),
    files={
        "app/__init__.py": "",
        "app/notify.py": "def send(msg):\n    return f'sent:{msg}'\n",
        "app/jobs.py": "def nightly():\n    return 0\n",
    },
    a=Edit(
        "a", "Route notifications through a channel",
        ("app/notify.py::function::send",),
        {"app/notify.py::function::send":
         "def send(msg, channel):\n    return f'{channel}:{msg}'\n"},
    ),
    b=Edit(
        "b", "Send the nightly report",
        ("app/jobs.py::function::nightly",),
        {"app/jobs.py::function::nightly":
         "def nightly():\n    from app.notify import send\n\n    return send('report')\n"},
        retry={"app/jobs.py::function::nightly":
               "def nightly():\n    from app.notify import send\n\n"
               "    return send('report', 'email')\n"},
    ),
    oracle="def check():\n    from app.jobs import nightly\n    nightly()\n",
)

# -- shape 3: behaviour change with an identical signature -----------------

_S3 = Scenario(
    shape=3,
    name="behaviour change, same signature",
    summary=(
        "A makes get_user return None instead of raising KeyError; B writes "
        "user_exists (and its test) against the raising contract."
    ),
    files={
        "svc/__init__.py": "",
        "svc/users.py": "_DB = {1: 'ann'}\n\n\ndef get_user(uid):\n    return _DB[uid]\n",
    },
    a=Edit(
        "a", "Return None for unknown users",
        ("svc/users.py::function::get_user",),
        {"svc/users.py::function::get_user":
         "def get_user(uid):\n    return _DB.get(uid)\n"},
    ),
    b=Edit(
        "b", "Add user_exists with a test",
        ("svc/exists.py", "tests/test_exists.py"),
        {
            "svc/exists.py": (
                "from svc.users import get_user\n\n\n"
                "def user_exists(uid):\n"
                "    try:\n        get_user(uid)\n    except KeyError:\n"
                "        return False\n    return True\n"
            ),
            "tests/test_exists.py": (
                "from svc.exists import user_exists\n\n\n"
                "def test_unknown_user_does_not_exist():\n"
                "    assert not user_exists(99)\n"
            ),
        },
    ),
    oracle=(
        "def check():\n"
        "    try:\n        from svc.exists import user_exists\n"
        "    except ImportError:\n        return\n"
        "    assert user_exists(1) and not user_exists(99)\n"
    ),
    needs=("impact_tests",),
)

# -- shape 4: deletion or rename -------------------------------------------

_S4 = Scenario(
    shape=4,
    name="deletion / rename",
    summary=(
        "A renames helpers.slugify to make_slug; B adds helpers.slugify(...) "
        "through a module alias, which the from-import check never reads."
    ),
    files={
        "lib/__init__.py": "",
        "lib/helpers.py": "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
    },
    a=Edit(
        "a", "Rename slugify to make_slug",
        ("lib/helpers.py::function::slugify",),
        {"lib/helpers.py::function::slugify":
         "def make_slug(text):\n    return text.lower().replace(' ', '-')\n"},
    ),
    b=Edit(
        "b", "Add title_slug",
        ("lib/report.py",),
        {"lib/report.py":
         "from lib import helpers\n\n\ndef title_slug(title):\n"
         "    return helpers.slugify(title)\n"},
    ),
    oracle=(
        "def check():\n"
        "    try:\n        from lib.report import title_slug\n"
        "    except ImportError:\n        return\n"
        "    assert title_slug('A B') == 'a-b'\n"
    ),
)

# -- shape 5: override the precision rules skip ----------------------------

_S5 = Scenario(
    shape=5,
    name="override against a changed base",
    summary=(
        "A changes Repo.save(self) to save(self, force) and updates its caller; "
        "B writes CacheRepo(Repo).save(self) against the old base."
    ),
    files={
        "store/__init__.py": "",
        "store/base.py": (
            "class Repo:\n"
            "    def save(self):\n"
            "        return 'base'\n\n\n"
            "def persist_all(repos):\n"
            "    return [r.save() for r in repos]\n"
        ),
    },
    a=Edit(
        "a", "Make save take a force flag",
        ("store/base.py::method::Repo.save", "store/base.py::function::persist_all"),
        {
            "store/base.py::method::Repo.save":
                "def save(self, force):\n    return 'base'\n",
            "store/base.py::function::persist_all":
                "def persist_all(repos):\n    return [r.save(True) for r in repos]\n",
        },
    ),
    b=Edit(
        "b", "Add a caching repository",
        ("store/cache.py",),
        {"store/cache.py":
         "from store.base import Repo\n\n\nclass CacheRepo(Repo):\n"
         "    def save(self):\n        return 'cache'\n"},
    ),
    oracle=(
        "def check():\n"
        "    from store.base import Repo, persist_all\n"
        "    repos = [Repo()]\n"
        "    try:\n        from store.cache import CacheRepo\n"
        "        repos.append(CacheRepo())\n"
        "    except ImportError:\n        pass\n"
        "    persist_all(repos)\n"
    ),
)

# -- shape 6: duplicate registration keys ----------------------------------

_ROUTES = "web/routes.py::function::_register_all"
_S6 = Scenario(
    shape=6,
    name="duplicate registration key",
    summary=(
        "A and B each register a handler under '/users' in the shared route "
        "table; both lines land and the second silently wins."
    ),
    files={
        "web/__init__.py": "",
        "web/routes.py": (
            "TABLE = {}\n\n\n"
            "def register(key, fn):\n    TABLE[key] = fn\n\n\n"
            "def users_index():\n    return 'users'\n\n\n"
            "def admin_users():\n    return 'admin'\n\n\n"
            "def _register_all() -> None:\n"
            '    """Every route."""\n'
            "    raise NotImplementedError\n"
        ),
    },
    a=Edit("a", "Route /users to users_index", (_ROUTES,),
           {_ROUTES: _append(_ROUTES, 'register("/users", users_index)')}),
    b=Edit(
        "b", "Route the admin user list", (_ROUTES,),
        {_ROUTES: _append(_ROUTES, 'register("/users", admin_users)')},
        retry={_ROUTES: _append(_ROUTES, 'register("/admin/users", admin_users)')},
    ),
    oracle=(
        "def check():\n"
        "    import web.routes as routes\n"
        "    calls = []\n"
        "    routes.register = lambda key, fn: calls.append(key)\n"
        "    try:\n        routes._register_all()\n"
        "    except NotImplementedError:\n        pass\n"
        "    assert len(calls) == len(set(calls)), calls\n"
    ),
)

# -- shape 7: order-dependent shared table ---------------------------------

_CHAIN = "web/middleware.py::function::_install"
_S7 = Scenario(
    shape=7,
    name="order-dependent table",
    summary=(
        "A adds require_auth and B adds audit to a middleware chain; audit "
        "must run after auth, and the chain's entries do not commute."
    ),
    files={
        "web/__init__.py": "",
        "web/middleware.py": (
            "CHAIN = []\n\n\n"
            "def use(fn):\n    CHAIN.append(fn)\n\n\n"
            "def cors(req):\n    return req\n\n\n"
            "def require_auth(req):\n    req['user'] = 'ann'\n    return req\n\n\n"
            "def audit(req):\n    req['seen'] = req.get('user', 'anonymous')\n"
            "    return req\n\n\n"
            "def _install() -> None:\n"
            '    """Build the middleware chain."""\n'
            "    use(cors)\n"
        ),
    },
    # Task ids sort audit first, so without coordination audit lands first.
    a=Edit(
        "auth", "Add require_auth; it must run before anything that reads the user",
        (_CHAIN,),
        {_CHAIN: _insert_before(_CHAIN, "use(require_auth)", "use(audit)")},
    ),
    b=Edit("audit", "Add the audit middleware", (_CHAIN,),
           {_CHAIN: _append(_CHAIN, "use(audit)")}),
    oracle=(
        "def check():\n"
        "    import web.middleware as mw\n"
        "    mw._install()\n"
        "    if mw.require_auth in mw.CHAIN and mw.audit in mw.CHAIN:\n"
        "        assert mw.CHAIN.index(mw.require_auth) < mw.CHAIN.index(mw.audit)\n"
    ),
)

# -- shape 8: shared structure change --------------------------------------

_S8 = Scenario(
    shape=8,
    name="new required field vs new construction",
    summary=(
        "A adds a required currency field to the Order dataclass; B writes "
        "make_order() constructing Order(1, 100) elsewhere."
    ),
    files={
        "orders/__init__.py": "",
        "orders/models.py": (
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\nclass Order:\n    id: int\n    total: int\n"
        ),
    },
    a=Edit(
        "a", "Add a currency to Order",
        ("orders/models.py::class::Order",),
        {"orders/models.py::class::Order":
         "@dataclass\nclass Order:\n    id: int\n    total: int\n    currency: str\n"},
    ),
    b=Edit(
        "b", "Add a factory for test orders",
        ("orders/shop.py",),
        {"orders/shop.py":
         "from orders.models import Order\n\n\ndef make_order():\n"
         "    return Order(1, 100)\n"},
    ),
    oracle=(
        "def check():\n"
        "    try:\n        from orders.shop import make_order\n"
        "    except ImportError:\n        return\n"
        "    make_order()\n"
    ),
)

# -- shape 9: duplicate implementations ------------------------------------

_NORM = "def _normalize_email(email):\n    return email.strip().lower()\n"
_S9 = Scenario(
    shape=9,
    name="duplicate implementation",
    summary=(
        "A and B each write a private _normalize_email in different modules; "
        "both are correct and the codebase now has two copies."
    ),
    files={
        "crm/__init__.py": "",
        "crm/users.py": "def create_user(email):\n    return {'email': email}\n",
        "crm/contacts.py": "def add_contact(email):\n    return {'email': email}\n",
    },
    a=Edit("a", "Normalize user emails", ("crm/users.py",),
           {"crm/users.py": _NORM + "\n\ndef create_user(email):\n"
                            "    return {'email': _normalize_email(email)}\n"}),
    b=Edit("b", "Normalize contact emails", ("crm/contacts.py",),
           {"crm/contacts.py": _NORM + "\n\ndef add_contact(email):\n"
                               "    return {'email': _normalize_email(email)}\n"}),
    oracle=(
        "def check():\n"
        "    import ast, pathlib\n"
        "    seen = {}\n"
        "    for path in sorted(pathlib.Path('crm').glob('*.py')):\n"
        "        for node in ast.parse(path.read_text()).body:\n"
        "            if isinstance(node, ast.FunctionDef) and node.name.startswith('_'):\n"
        "                body = ast.dump(ast.Module(body=node.body, type_ignores=[]))\n"
        "                assert (node.name, body) not in seen, (path, seen)\n"
        "                seen[(node.name, body)] = path\n"
    ),
)

SCENARIOS: tuple[Scenario, ...] = (_S1, _S2, _S3, _S4, _S5, _S6, _S7, _S8, _S9)
