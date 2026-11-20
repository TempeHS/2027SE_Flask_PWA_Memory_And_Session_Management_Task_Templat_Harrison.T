"""Flask integration for the Memory & Session Lab.

attach_lab(app) does three things to the student's Flask app:

1. Registers a Flask blueprint (a bundle of extra routes) under /lab: the
   dashboard page, the JSON it polls every second, the per-tab probe and the
   add customers and reset buttons.
2. Adds a hook that runs before every request and another that runs after
   every response. Together they notice cookies being set or deleted and
   sessions changing ID when a user signs in or out.
3. Adds a small probe script to every HTML page the student app sends. The
   probe reports each browser tab's measurements back to the lab.

All the lab's own memory is kept in bounded or pruned collections, so the lab
does not leak memory into the measurements it is taking.
"""

import collections
import gc
import numbers
import random
import re
import threading
import time
import tracemalloc

from flask import Blueprint
from flask import current_app
from flask import g
from flask import jsonify
from flask import render_template
from flask import request
from flask import session

from . import measure

MAX_CUSTOMERS_PER_CLICK = 100000
CSRF_PATTERN = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
ITEM_PATTERN = re.compile(r'name="itemID"\s+value="(\d+)"')
FIRST_NAMES = ["Ava", "Liam", "Mia", "Noah", "Zoe", "Jack", "Ruby", "Priya", "Kai"]

ATTRIBUTION_INTERVAL = 4  # seconds between tracemalloc snapshots
CONTAINER_INTERVAL = 2  # seconds between deep-size scans of module containers
MODULE_INTERVAL = 5  # seconds between rescans of sys.modules
TAB_TIMEOUT = 8  # seconds without a probe before a tab is treated as closed
MAX_SESSION_TILES = 200
SMALL_SESSION_BYTES = 2048
UNBOUNDED_ITEMS_LIMIT = 200
STRONG_ID_BITS = 128
USER_KEYS = ("name", "nickname", "username", "user")
TAB_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{4,40}$")
PROBE_TAGS = (
    '<link rel="stylesheet" href="/lab/static/lab.css" />'
    '<script src="/lab/static/probe.js" defer></script>'
)

bp = Blueprint(
    "memory_lab",
    __name__,
    url_prefix="/lab",
    template_folder="templates",
    static_folder="static",
)


class LabState:
    """Everything the lab remembers between requests.

    Every collection here is either a deque with a maximum length or is pruned
    regularly, so the lab's own memory use stays flat.
    """

    def __init__(self):
        """Create the lock and empty collections."""
        self.lock = threading.Lock()
        self.modules = []
        self.modules_time = 0.0
        self.reset()

    def reset(self):
        """Forget all tabs, cookies and session events seen so far."""
        self.tabs = {}
        self.cookies = {}
        self.session_events = {}
        self.login_events = collections.deque(maxlen=20)
        self.logout_events = collections.deque(maxlen=20)
        self.cached = {}


state = LabState()
EXEMPT_VIEWS = set()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _modules():
    """Return the student's modules, rescanning sys.modules every few seconds."""
    now = time.time()
    with state.lock:
        if now - state.modules_time > MODULE_INTERVAL:
            state.modules = measure.student_modules(current_app.root_path)
            state.modules_time = now
        return list(state.modules)


def _sessions():
    """Return the student's SESSIONS dictionary (or an empty one if not found)."""
    return measure.find_session_store(_modules())[2]


def _user_of(data):
    """Return the signed-in user's name stored in a session, or None."""
    if not isinstance(data, dict):
        return None
    for key in USER_KEYS:
        if data.get(key):
            return str(data[key])[:20]
    return None


def _cached(key, interval, compute):
    """Return a recent result of compute(), only re-running it every interval.

    Some measurements are slow (a tracemalloc snapshot looks at every block of
    memory), so they are reused for a few seconds rather than repeated every
    time the dashboard asks.
    """
    now = time.time()
    with state.lock:
        hit = state.cached.get(key)
        if hit and now - hit[0] < interval:
            return hit[1]
    value = compute()
    with state.lock:
        state.cached[key] = (now, value)
    return value


def _number(value):
    """Return value if it is a real number (not a bool), otherwise None."""
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return value
    return None


def _events(pid):
    """Return the dictionary of events recorded for one session label."""
    with state.lock:
        if len(state.session_events) > 1000:
            state.session_events.clear()
        return state.session_events.setdefault(pid, {})


def _session_for_request():
    """Find which student session the current request's cookies belong to.

    The lab does not need to know the cookie's name: any cookie whose value is
    a key in SESSIONS must be the session cookie.
    """
    sessions = _sessions()
    for value in request.cookies.values():
        if value in sessions:
            return value, sessions[value]
    return None, None


def _ensure_csrf_exemption():
    """Let the probe POST without a CSRF token.

    The probe runs on every student page and only sends measurements, so it is
    exempted from Flask-WTF's CSRF check. The dashboard's buttons still need a
    valid CSRF token.
    """
    csrf = current_app.extensions.get("csrf")
    if csrf is not None and probe not in EXEMPT_VIEWS:
        csrf.exempt(probe)
        EXEMPT_VIEWS.add(probe)


# ---------------------------------------------------------------------------
# Request hooks (run around every student request)
# ---------------------------------------------------------------------------


def _before_request():
    """Remember who was signed in to the request's session before it runs.

    _after_request() compares this with the session afterwards to detect a
    sign in or sign out, and whether the session ID changed.
    """
    _ensure_csrf_exemption()
    if request.path.startswith("/lab"):
        return
    sessions = _sessions()
    before = {}
    for value in request.cookies.values():
        if value in sessions:
            before[value] = _user_of(sessions[value])
    g.memory_lab_before = before


def _after_request(response):
    """Inspect a student response, then add the probe script to HTML pages.

    Any error here is logged and ignored: the lab must never break the app.
    """
    if request.path.startswith("/lab"):
        return response
    try:
        headers = response.headers.getlist("Set-Cookie")
        cookies = [measure.parse_set_cookie(header) for header in headers]
        _record_cookies(cookies)
        _record_flask_session_cookie()
        _record_session_events(cookies)
        _inject_probe(response)
    except Exception:  # pylint: disable=broad-except
        current_app.logger.exception("Memory lab request hook failed")
    return response


def _record_cookies(cookies):
    """Remember the security flags of each cookie the server sets or deletes.

    Only flags are stored, never cookie values.
    """
    sessions = _sessions()
    signed_name = current_app.config.get("SESSION_COOKIE_NAME", "session")
    now = time.time()
    with state.lock:
        for name, value, info in cookies:
            previous = state.cookies.get(name, {})
            if info["deleted"]:
                previous["deleted_at"] = now
                state.cookies[name] = previous
                continue
            if value in sessions:
                kind = "Server session ID"
            elif name == signed_name:
                kind = "Flask signed session (data stored in the browser)"
            else:
                kind = previous.get("kind", "Other cookie")
            state.cookies[name] = dict(info, kind=kind, set_at=now, deleted_at=None)


def _record_flask_session_cookie():
    """Remember the flags of Flask's own signed "session" cookie.

    Flask writes that cookie after every after_request hook has run, so the
    lab cannot see it in the response. Instead it asks Flask's session
    interface for the exact settings Flask will use (from app.config).
    """
    app = current_app._get_current_object()  # pylint: disable=protected-access
    interface = app.session_interface
    if not session or not interface.should_set_cookie(app, session):
        return
    lifetime = None
    if session.permanent:
        lifetime = str(int(app.permanent_session_lifetime.total_seconds()))
    info = {
        "httponly": interface.get_cookie_httponly(app),
        "secure": interface.get_cookie_secure(app),
        "samesite": interface.get_cookie_samesite(app),
        "lifetime": lifetime,
        "kind": "Flask signed session (data stored in the browser)",
        "set_at": time.time(),
        "deleted_at": None,
    }
    with state.lock:
        state.cookies[interface.get_cookie_name(app)] = info


def _record_session_events(cookies):
    """Detect sign in and sign out, and how the session ID was handled.

    - Signed in and the ID stayed the same: session fixation risk.
    - Signed in and a new ID was issued: the ID was regenerated (good).
    - Signed out but the session still exists: the session was kept (bad).
    - Signed out, the session is gone and the cookie deleted: destroyed (good).
    """
    sessions = _sessions()
    before = g.get("memory_lab_before", {})
    new_ids = [
        value for _, value, info in cookies if not info["deleted"] and value in sessions
    ]
    cookie_deleted = any(info["deleted"] for _, _, info in cookies)
    for old_id, old_user in before.items():
        if old_id in sessions:
            new_user = _user_of(sessions[old_id])
            if old_user is None and new_user:
                _events(measure.public_id(old_id))["fixation"] = True
                with state.lock:
                    state.login_events.append("fixation")
            elif old_user and not new_user:
                with state.lock:
                    state.logout_events.append("kept")
            continue
        for new_id in new_ids:
            if _user_of(sessions.get(new_id)) and not old_user:
                _events(measure.public_id(new_id))["regenerated"] = True
                with state.lock:
                    state.login_events.append("regenerated")
        if cookie_deleted and not new_ids:
            with state.lock:
                state.logout_events.append("destroyed")


def _inject_probe(response):
    """Add the probe script and its stylesheet just before </body>."""
    if response.status_code != 200 or response.mimetype != "text/html":
        return
    if response.direct_passthrough or response.is_streamed:
        return
    body = response.get_data(as_text=True)
    if "</body>" in body and "/lab/static/probe.js" not in body:
        response.set_data(body.replace("</body>", PROBE_TAGS + "</body>", 1))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.get("")
def dashboard():
    """Show the lab dashboard page."""
    return render_template("memory_lab/dashboard.html")


@bp.post("/probe")
def probe():
    """Receive a heartbeat from the probe running in a student page.

    Each tab reports its tab ID (kept in sessionStorage, so unique per tab),
    the page it is showing, its DOM node count and its sessionStorage size.
    The reply tells the probe which session the tab belongs to, so it can show
    the coloured badge.
    """
    payload = request.get_json(silent=True) or {}
    tab_id = str(payload.get("tab", ""))
    if not TAB_ID_PATTERN.match(tab_id):
        return jsonify(error="invalid tab id"), 400
    session_id, data = _session_for_request()
    pid = measure.public_id(session_id) if session_id else None
    now = time.time()
    page_load = _number(payload.get("pageLoad")) or 0
    dom = int(_number(payload.get("dom")) or 0)
    with state.lock:
        tab = state.tabs.get(tab_id)
        if tab is None or tab.get("page_load") != page_load:
            tab = {
                "tab": tab_id,
                "first_seen": now,
                "first_dom": dom,
                "page_load": page_load,
            }
            state.tabs[tab_id] = tab
        tab.update(
            session=pid,
            path=str(payload.get("path", ""))[:80],
            dom=dom,
            session_storage=int(_number(payload.get("sessionStorage")) or 0),
            heap=_number(payload.get("heap")),
            last_seen=now,
        )
        _prune_tabs(now)
    return jsonify(session=pid, user=_user_of(data))


def _prune_tabs(now):
    """Forget tabs that have not reported for a while (they were closed).

    The caller must hold state.lock.
    """
    stale = [k for k, v in state.tabs.items() if now - v["last_seen"] > TAB_TIMEOUT]
    for key in stale:
        del state.tabs[key]


def _simulate_customer(client):
    """Act like one new customer on their own phone.

    They view the menu, add 1 to 3 items and about half sign in. The requests
    go through the real student app with a fresh cookie jar, so each customer
    gets their own session, exactly like a new device.
    """
    page = client.get("/", base_url="https://localhost").get_data(as_text=True)
    token = CSRF_PATTERN.search(page)
    item_ids = sorted(set(ITEM_PATTERN.findall(page)))
    if not token or not item_ids:
        return
    form = {"csrf_token": token.group(1)}
    base = "https://localhost"
    # Browsers send a Referer with forms; Flask-WTF rejects HTTPS posts without one.
    headers = {"Referer": base + "/"}
    count = min(len(item_ids), random.randint(1, 3))
    for item_id in random.sample(item_ids, k=count):
        data = dict(form, itemID=item_id)
        client.post("/add", data=data, headers=headers, base_url=base)
    if random.random() < 0.5:
        data = dict(form, name=random.choice(FIRST_NAMES))
        client.post("/login", data=data, headers=headers, base_url=base)


@bp.post("/simulate")
def simulate():
    """Add customers, each with their own session, on top of any already there."""
    payload = request.get_json(silent=True) or {}
    try:
        customers = int(payload.get("customers", 10))
    except (TypeError, ValueError):
        customers = 10
    customers = max(1, min(customers, MAX_CUSTOMERS_PER_CLICK))
    before = set(_sessions().keys())
    app = current_app._get_current_object()  # pylint: disable=protected-access

    def visit():
        for _ in range(customers):
            _simulate_customer(app.test_client())

    # A separate thread gets its own Flask context, so the student's g is not
    # shared with this request.
    worker = threading.Thread(target=visit)
    worker.start()
    worker.join()
    created = [key for key in _sessions().keys() if key not in before]
    for session_id in created:
        _events(measure.public_id(session_id))["simulated"] = True
    return jsonify(customers=customers, sessions_created=len(created))


@bp.post("/reset")
def reset():
    """Clear all sessions, caches and lab history to start a fresh experiment.

    gc.collect() asks Python's garbage collector to free anything that is no
    longer referenced, so the memory chart drops straight away.
    """
    _sessions().clear()
    for _, function in measure.find_caches(_modules()):
        function.cache_clear()
    with state.lock:
        state.reset()
    gc.collect()
    if tracemalloc.is_tracing():
        tracemalloc.reset_peak()
    return jsonify(reset=True)


@bp.get("/stats")
def stats():
    """Return every measurement the dashboard displays, as JSON.

    The dashboard calls this once a second.
    """
    now = time.time()
    root = current_app.root_path
    modules = _modules()
    store_name, store_module, sessions = measure.find_session_store(modules)

    # Act like a scheduled clean-up job so expiry is visible with no traffic.
    cleanup = getattr(store_module, "remove_expired_sessions", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:  # pylint: disable=broad-except
            current_app.logger.exception("remove_expired_sessions() failed")

    traced, peak = (0, 0)
    if tracemalloc.is_tracing():
        traced, peak = tracemalloc.get_traced_memory()
    attribution = _cached(
        "attribution", ATTRIBUTION_INTERVAL, lambda: measure.attribute_memory(root)
    )
    containers = _cached(
        "containers", CONTAINER_INTERVAL, lambda: measure.describe_containers(modules)
    )
    caches = measure.describe_caches(modules)
    idle_timeout = _number(getattr(store_module, "IDLE_TIMEOUT", None))
    absolute_timeout = _number(getattr(store_module, "ABSOLUTE_TIMEOUT", None))

    with state.lock:
        _prune_tabs(now)
        tabs = [dict(tab) for tab in state.tabs.values()]
        cookies = [dict(info, name=name) for name, info in state.cookies.items()]
        login_events = list(state.login_events)
        logout_events = list(state.logout_events)

    session_rows = _describe_sessions(sessions, tabs, now)
    for tab in tabs:
        tab["age"] = now - tab["first_seen"]
        tab["dom_growth"] = tab["dom"] - tab["first_dom"]
        tab.pop("page_load", None)

    return jsonify(
        time=now,
        server={
            "traced": traced,
            "peak": peak,
            "rss": measure.process_rss(),
            "attributed": attribution["total"],
        },
        attribution=attribution["lines"],
        containers=containers,
        caches=caches,
        store={
            "module": store_name,
            "idle_timeout": idle_timeout,
            "absolute_timeout": absolute_timeout,
            "count": len(sessions),
        },
        sessions=session_rows,
        cookies=cookies,
        tabs=tabs,
        checks=_server_checks(
            session_rows,
            containers,
            caches,
            cookies,
            idle_timeout,
            login_events,
            logout_events,
        ),
    )


def _describe_sessions(sessions, tabs, now):
    """Build one row per session for the dashboard's session tiles.

    Real session IDs are never sent to the browser: each session is shown by
    its public_id() label instead.
    """
    your_cookie_values = set(request.cookies.values())
    rows = []
    for session_id, data in list(sessions.items())[:MAX_SESSION_TILES]:
        pid = measure.public_id(session_id)
        bits, sequential = measure.id_strength(session_id)
        details = data if isinstance(data, dict) else {}
        created = _number(details.get("created"))
        last_seen = _number(details.get("last_seen"))
        events = _events(pid)
        rows.append(
            {
                "id": pid,
                "you": session_id in your_cookie_values,
                "bits": bits,
                "sequential": sequential,
                "bytes": measure.deep_size(data),
                "keys": sorted(str(key) for key in details.keys()),
                "user": _user_of(details),
                "age": now - created if created else None,
                "idle": now - last_seen if last_seen else None,
                "tabs": sorted(t["tab"] for t in tabs if t.get("session") == pid),
                "simulated": bool(events.get("simulated")),
                "regenerated": bool(events.get("regenerated")),
                "fixation": bool(events.get("fixation")),
            }
        )
    rows.sort(key=lambda row: (not row["you"], row["simulated"], -(row["age"] or 0)))
    return rows


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


def _check(key, group, title, status, detail):
    """Build one scorecard entry. status is "pass", "fail" or "waiting"."""
    return {
        "key": key,
        "group": group,
        "title": title,
        "status": status,
        "detail": detail,
    }


def _server_checks(
    sessions, containers, caches, cookies, idle_timeout, login_events, logout_events
):
    """Work out the server memory and session management scorecard checks.

    Each check matches a README step. The client memory checks are worked out
    in the browser by dashboard.js, because only the browser can see its own
    storage.
    """
    return [
        _check_session_size(sessions),
        _check_bounded(containers),
        _check_shared_cache(caches),
        _check_id_strength(sessions),
        _check_cookie_flags(cookies),
        _check_cookie_lifetime(cookies),
        _check_expiry(idle_timeout),
        _check_login(login_events),
        _check_logout(logout_events),
    ]


def _check_session_size(sessions):
    """README Steps 4 and 5: sessions should hold only per-user data."""
    title = "Sessions store only what they need"
    if not sessions:
        return _check(
            "session_size",
            "Server memory",
            title,
            "waiting",
            "Open the menu to create a session.",
        )
    average = sum(row["bytes"] for row in sessions) / len(sessions)
    status = "pass" if average < SMALL_SESSION_BYTES else "fail"
    detail = f"Average session uses {average / 1024:.1f} KB."
    return _check("session_size", "Server memory", title, status, detail)


def _check_bounded(containers):
    """README Step 7: lists that live for the whole server run must have a limit."""
    title = "Long-lived collections are bounded"
    growing = [
        row
        for row in containers
        if row["type"] in ("list", "deque")
        and not row["bounded"]
        and row["items"] > UNBOUNDED_ITEMS_LIMIT
    ]
    bounded = [row for row in containers if row["bounded"]]
    if growing:
        names = ", ".join(row["name"] for row in growing)
        return _check(
            "bounded",
            "Server memory",
            title,
            "fail",
            f"Unbounded and growing: {names}.",
        )
    if bounded:
        names = ", ".join(f"{row['name']} (max {row['maxlen']})" for row in bounded)
        return _check("bounded", "Server memory", title, "pass", f"Bounded: {names}.")
    return _check(
        "bounded",
        "Server memory",
        title,
        "waiting",
        "Use the app for a minute to see if any list keeps growing.",
    )


def _check_shared_cache(caches):
    """README Step 6: shared data should be cached once, with a size limit."""
    title = "Shared data is cached once"
    bounded = [row for row in caches if row["maxsize"] is not None]
    if any(row["hits"] > 0 for row in bounded):
        row = bounded[0]
        detail = (
            f"{row['name']}: {row['hits']} hits, "
            f"{row['currsize']}/{row['maxsize']} entries."
        )
        return _check("shared_cache", "Server memory", title, "pass", detail)
    if bounded:
        detail = "Cache found. Load a page twice to see a cache hit."
        return _check("shared_cache", "Server memory", title, "waiting", detail)
    detail = "No shared, bounded cache found. Shared data is copied or re-queried."
    return _check("shared_cache", "Server memory", title, "fail", detail)


def _check_id_strength(sessions):
    """README Step 11: session IDs must be long and random."""
    title = "Session IDs are unpredictable"
    real = [row for row in sessions if not row["simulated"]] or sessions
    if not real:
        detail = "Open the menu to create a session."
        return _check("id_strength", "Session management", title, "waiting", detail)
    weakest = min(real, key=lambda row: row["bits"])
    if weakest["sequential"]:
        detail = "Session IDs are counting numbers, so they are easy to guess."
        return _check("id_strength", "Session management", title, "fail", detail)
    status = "pass" if weakest["bits"] >= STRONG_ID_BITS else "fail"
    detail = f"Weakest ID has about {weakest['bits']} bits of randomness."
    return _check("id_strength", "Session management", title, status, detail)


def _check_cookie_flags(cookies):
    """README Step 13: every cookie should be HttpOnly, Secure and SameSite."""
    title = "Cookies have security flags"
    live = [cookie for cookie in cookies if cookie.get("kind")]
    if not live:
        return _check(
            "cookie_flags",
            "Session management",
            title,
            "waiting",
            "No cookies seen yet.",
        )
    missing = []
    for cookie in live:
        flags = [
            flag
            for flag, present in (
                ("HttpOnly", cookie.get("httponly")),
                ("Secure", cookie.get("secure")),
                ("SameSite", cookie.get("samesite")),
            )
            if not present
        ]
        if flags:
            missing.append(f"{cookie['name']} missing {', '.join(flags)}")
    if missing:
        return _check(
            "cookie_flags", "Session management", title, "fail", "; ".join(missing)
        )
    detail = "All cookies are HttpOnly, Secure and SameSite."
    return _check("cookie_flags", "Session management", title, "pass", detail)


def _check_cookie_lifetime(cookies):
    """README Step 13: the session cookie should expire."""
    title = "Session cookie has a lifetime"
    session_cookies = [c for c in cookies if c.get("kind") == "Server session ID"]
    if not session_cookies:
        detail = "No session cookie seen yet."
        return _check("cookie_lifetime", "Session management", title, "waiting", detail)
    lifetime = session_cookies[0].get("lifetime")
    if lifetime:
        detail = f"Expires after {lifetime} seconds."
        return _check("cookie_lifetime", "Session management", title, "pass", detail)
    detail = "The session cookie has no expiry, so the browser decides when to drop it."
    return _check("cookie_lifetime", "Session management", title, "fail", detail)


def _check_expiry(idle_timeout):
    """README Step 12: the server should remove idle sessions."""
    title = "Sessions expire on the server"
    if idle_timeout:
        detail = f"Idle sessions are removed after {idle_timeout:g} seconds."
        return _check("expiry", "Session management", title, "pass", detail)
    detail = "Sessions never expire, so abandoned sessions stay in memory forever."
    return _check("expiry", "Session management", title, "fail", detail)


def _check_login(login_events):
    """README Step 14: signing in should issue a new session ID."""
    title = "Session ID changes on sign in"
    if not login_events:
        detail = "Sign in with a name to test."
        return _check("login", "Session management", title, "waiting", detail)
    if login_events[-1] == "regenerated":
        detail = "A new session ID was issued when the user signed in."
        return _check("login", "Session management", title, "pass", detail)
    detail = "A user signed in but kept the same session ID (session fixation risk)."
    return _check("login", "Session management", title, "fail", detail)


def _check_logout(logout_events):
    """README Step 15: signing out should destroy the session and its cookie."""
    title = "Signing out destroys the session"
    if not logout_events:
        detail = "Sign in, then press Sign out to test."
        return _check("logout", "Session management", title, "waiting", detail)
    if logout_events[-1] == "destroyed":
        detail = "Signing out destroyed the session and deleted the cookie."
        return _check("logout", "Session management", title, "pass", detail)
    detail = "Signing out kept the session (and its memory) on the server."
    return _check("logout", "Session management", title, "fail", detail)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attach_lab(app):
    """Attach the lab dashboard (/lab) and its request hooks to a Flask app.

    Flask runs before_request hooks first to last, and after_request hooks
    last to first. Inserting the lab's hooks at position 0 means the lab sees
    each request before the student's code and each response after it, so it
    always sees the final cookies.
    """
    app.register_blueprint(bp)
    app.before_request_funcs.setdefault(None, []).insert(0, _before_request)
    app.after_request_funcs.setdefault(None, []).insert(0, _after_request)
    return app
