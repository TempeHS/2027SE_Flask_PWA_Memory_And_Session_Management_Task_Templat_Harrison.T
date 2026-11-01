"""Memory & Session Lab: a live teaching dashboard for the School Canteen app.

You do not need to read or edit this package to complete the task, but you are
welcome to. It is attached to the Flask app in main.py with:

    from memory_lab import attach_lab
    attach_lab(app)

and then serves a dashboard at /lab.

How it works, in short:

- measure.py takes measurements of the running Python process: how much memory
  is in use (using the standard library module tracemalloc), which lines of the
  student code allocated it, and how big the student's data structures are.
- lab.py plugs into Flask. It watches every request and response (to see
  cookies and session changes), adds a small "probe" script to every student
  page (to measure each browser tab), and provides the JSON the dashboard
  reads every second.
- templates/ and static/ hold the dashboard page, its JavaScript and the probe.

The lab must never be deployed to a real website: it shows private details of
every session so that they can be studied.
"""

import tracemalloc

TRACE_FRAMES = 12

# tracemalloc only records memory allocated after it starts, so start it as soon
# as main.py imports the lab, before the student app has created its data.
if not tracemalloc.is_tracing():
    tracemalloc.start(TRACE_FRAMES)

from .lab import attach_lab  # noqa: E402  pylint: disable=wrong-import-position

__all__ = ["attach_lab"]
