"""Measurements of the running Python server, used by the lab dashboard.

Python stores every value (numbers, strings, lists, dictionaries) as an object
in an area of memory called the heap. This module answers three questions about
that memory:

1. How much is in use right now? The standard library module tracemalloc
   records every block of memory Python allocates, and where in the code the
   allocation happened.
2. Which lines of the student's code are responsible? attribute_memory() walks
   back through each allocation's call stack to the most recent line in a
   student file.
3. How big are the student's data structures? deep_size() adds up the size of
   a container and everything inside it.

It also checks how hard session IDs would be to guess and reads cookie flags.
"""

import collections
import hashlib
import linecache
import math
import os
import string
import sys
import tracemalloc
import types

LAB_DIR = os.path.dirname(os.path.abspath(__file__))
CONTAINER_TYPES = (dict, list, set, frozenset, tuple, collections.deque)
IGNORED_PATH_PARTS = ("site-packages", "dist-packages", f"{os.sep}.", "__pycache__")
IMPORT_MACHINERY = "<frozen importlib"


def is_student_file(path, root):
    """Return True if a source file belongs to the student app.

    Files inside the project folder count as student files, except for this
    lab, installed libraries and hidden folders such as a virtual environment.
    """
    if not path or path.startswith("<"):
        return False
    path = os.path.abspath(path)
    if not path.startswith(root + os.sep) or path.startswith(LAB_DIR + os.sep):
        return False
    relative = path[len(root) :]
    return not any(part in relative for part in IGNORED_PATH_PARTS)


def student_modules(root):
    """Return a list of (short name, module) for every imported student module.

    Python keeps every imported module in sys.modules. Module-level variables
    (such as SESSIONS or REQUEST_LOG) live as long as the module does, which
    for a web server is until the server stops.
    """
    found = []
    seen = set()
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not isinstance(module, types.ModuleType) or id(module) in seen:
            continue
        if path and path.endswith(".py") and is_student_file(path, root):
            seen.add(id(module))
            found.append((os.path.splitext(os.path.basename(path))[0], module))
    found.sort(key=lambda item: item[0])
    return found


def deep_size(obj):
    """Return the approximate number of bytes used by obj and everything in it.

    sys.getsizeof() only measures the container itself (for a list, just the
    list of references), not the objects it refers to. This function follows
    every reference inside dictionaries, lists, tuples, sets and deques and
    adds them up. Objects reached twice are only counted once, which is exactly
    why sharing one copy of data uses less memory than making many copies.
    """
    seen = set()
    total = 0
    stack = [obj]
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        try:
            total += sys.getsizeof(item)
        except TypeError:
            continue
        try:
            if isinstance(item, dict):
                for key, value in list(item.items()):
                    stack.append(key)
                    stack.append(value)
            elif isinstance(item, (list, tuple, set, frozenset, collections.deque)):
                stack.extend(list(item))
        except RuntimeError:
            # Another request changed the container while it was being copied.
            continue
    return total


def process_rss():
    """Return the resident set size (RSS) of the server process in bytes.

    RSS is the memory the operating system has given the process. It is larger
    than the Python heap (it includes Python itself and its libraries) and it
    rarely shrinks, because Python keeps freed memory to reuse later.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            pages = int(statm.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    try:
        import resource  # pylint: disable=import-outside-toplevel

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024
    except (ImportError, OSError):
        return None


def _raw_traces(snapshot):
    """Yield (size, frames) for every block in a snapshot, newest frame first.

    Reading the snapshot's internal tuples directly is many times faster than
    building a Trace object for each block, which matters when there are tens
    of thousands of blocks. The public API is used if the internals change.
    """
    raw = getattr(snapshot.traces, "_traces", None)
    if isinstance(raw, list):
        for trace in raw:
            yield trace[1], trace[2]
        return
    for trace in snapshot.traces:
        frames = [(frame.filename, frame.lineno) for frame in trace.traceback]
        yield trace.size, reversed(frames)


def _file_kind(filename, root):
    """Classify a source file as "student", "skip" (lab or import) or "other"."""
    if filename.startswith(LAB_DIR) or filename.startswith(IMPORT_MACHINERY):
        return "skip"
    return "student" if is_student_file(filename, root) else "other"


def attribute_memory(root, limit=8):
    """Work out which lines of student code allocated the memory still in use.

    tracemalloc records a short call stack (traceback) for every live block of
    memory. A block is usually allocated deep inside Flask, Jinja2 or Python
    itself, so for each block this walks back up the stack to the most recent
    line in a student file: the line that caused the allocation. Blocks
    allocated by the lab itself, or while importing modules, are skipped.

    The same few files appear in thousands of call stacks, so each file is
    classified once and the answer reused.

    Returns the total bytes attributed to student code and the top lines.
    """
    if not tracemalloc.is_tracing():
        return {"total": 0, "lines": []}
    snapshot = tracemalloc.take_snapshot()
    kinds = {}
    totals = {}
    for size, frames in _raw_traces(snapshot):
        for filename, lineno in frames:
            kind = kinds.get(filename)
            if kind is None:
                kind = kinds[filename] = _file_kind(filename, root)
            if kind == "skip":
                break
            if kind == "student":
                key = (filename, lineno)
                total, count = totals.get(key, (0, 0))
                totals[key] = (total + size, count + 1)
                break
    ranked = sorted(totals.items(), key=lambda item: item[1][0], reverse=True)
    lines = []
    for (filename, lineno), (size, count) in ranked[:limit]:
        lines.append(
            {
                "file": os.path.relpath(filename, root),
                "line": lineno,
                "code": linecache.getline(filename, lineno).strip()[:120],
                "bytes": size,
                "blocks": count,
            }
        )
    return {"total": sum(size for size, _ in totals.values()), "lines": lines}


def describe_containers(modules):
    """Describe every module-level list, dict, set, tuple and deque.

    These are the data structures that outlive a single request. A deque
    created with maxlen is "bounded": it can never hold more than maxlen items.
    """
    rows = []
    seen = set()
    for display, module in modules:
        for name, value in list(vars(module).items()):
            if name.startswith("_") or not isinstance(value, CONTAINER_TYPES):
                continue
            if id(value) in seen:
                continue
            seen.add(id(value))
            row = {
                "name": f"{display}.{name}",
                "type": type(value).__name__,
                "items": len(value),
                "bytes": deep_size(value),
                "bounded": isinstance(value, collections.deque)
                and value.maxlen is not None,
            }
            if isinstance(value, collections.deque):
                row["maxlen"] = value.maxlen
            rows.append(row)
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows


def find_caches(modules):
    """Return (name, function) for every cached function in the student app.

    Functions wrapped with functools.lru_cache have cache_info() and
    cache_clear() methods, which is how they are recognised.
    """
    found = []
    seen = set()
    for display, module in modules:
        for name, value in list(vars(module).items()):
            is_cache = hasattr(value, "cache_info") and hasattr(value, "cache_clear")
            if callable(value) and is_cache and id(value) not in seen:
                seen.add(id(value))
                owner = getattr(value, "__module__", display)
                found.append((f"{owner}.{getattr(value, '__name__', name)}", value))
    return found


def describe_caches(modules):
    """Report hits, misses and size for every cached function.

    A hit means the stored result was reused instead of running the function
    again. maxsize is the bound: None means the cache can grow forever.
    """
    rows = []
    for name, function in find_caches(modules):
        info = function.cache_info()
        rows.append(
            {
                "name": name,
                "hits": info.hits,
                "misses": info.misses,
                "maxsize": info.maxsize,
                "currsize": info.currsize,
            }
        )
    return rows


def find_session_store(modules):
    """Return (module name, module, sessions) for the module that defines SESSIONS.

    If no module defines a SESSIONS dictionary, an empty one is returned.
    """
    for display, module in modules:
        sessions = getattr(module, "SESSIONS", None)
        if isinstance(sessions, dict):
            return display, module, sessions
    return None, None, {}


def public_id(session_id):
    """Return a short label for a session that does not reveal the real ID.

    The label is the start of a SHA-256 hash of the ID. Hashing is one-way, so
    the label can be shown on the dashboard without letting anyone use it to
    take over the session. Six hex characters also make a handy colour.
    """
    return hashlib.sha256(str(session_id).encode()).hexdigest()[:6]


def id_strength(session_id):
    """Estimate how hard a session ID is to guess, in bits.

    Each extra bit doubles the number of guesses an attacker needs. 128 bits or
    more is considered impossible to guess. Returns (bits, sequential), where
    sequential is True for plain counting numbers like "1", "2", "3".
    """
    text = str(session_id)
    if not text:
        return 0, True
    if text.isdigit():
        return round(len(text) * math.log2(10)), True
    if set(text) <= set(string.hexdigits + "-"):
        return sum(1 for char in text if char != "-") * 4, False
    if set(text) <= set(string.ascii_letters + string.digits + "-_="):
        return sum(1 for char in text if char != "=") * 6, False
    pools = [
        string.digits,
        string.ascii_lowercase,
        string.ascii_uppercase,
        "-_",
        string.punctuation,
    ]
    pool_size = sum(len(pool) for pool in pools if any(c in pool for c in text))
    return round(len(text) * math.log2(max(pool_size, 2))), False


def parse_set_cookie(header):
    """Split a Set-Cookie header into its name, value and security flags.

    A cookie whose Max-Age is 0 (or whose expiry date is in 1970) is the
    server's way of telling the browser to delete it.
    """
    parts = [part.strip() for part in header.split(";")]
    name, _, value = parts[0].partition("=")
    attributes = {}
    for part in parts[1:]:
        key, _, attribute_value = part.partition("=")
        attributes[key.lower()] = attribute_value
    deleted = attributes.get("max-age") == "0" or "1970" in attributes.get(
        "expires", ""
    )
    return (
        name,
        value,
        {
            "httponly": "httponly" in attributes,
            "secure": "secure" in attributes,
            "samesite": attributes.get("samesite") or None,
            "lifetime": attributes.get("max-age") or attributes.get("expires") or None,
            "deleted": deleted,
        },
    )
