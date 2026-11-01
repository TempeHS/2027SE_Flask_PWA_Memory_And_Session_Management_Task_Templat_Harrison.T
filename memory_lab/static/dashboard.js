/**
 * Memory & Session Lab dashboard.
 *
 * Every second this script asks the server for /lab/stats (server memory,
 * sessions, cookies and open tabs) and measures this site's browser storage
 * itself (localStorage and the Cache API). It then redraws the charts,
 * scorecard, session tiles and tables.
 *
 * The dashboard follows its own advice: its chart history is bounded
 * (MAX_POINTS) and it replaces page elements instead of adding new ones.
 */
(() => {
  "use strict";

  const POLL_MS = 1000;
  const CLIENT_MS = 2000;
  const MAX_POINTS = 180;
  const DOM_GROWTH_LIMIT = 60;
  const LOCAL_STORAGE_KEY_LIMIT = 5;
  const MATURE_TAB_SECONDS = 20;
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

  const serverPoints = [];
  const clientPoints = [];
  let client = null;
  let lastClientMeasure = 0;

  const SERVER_SERIES = [
    { key: "traced", label: "Whole Python heap", colour: "#0d6efd" },
    { key: "attributed", label: "Allocated by your code", colour: "#dc3545" },
  ];
  const CLIENT_SERIES = [
    { key: "dom", label: "DOM nodes (all tabs)", colour: "#6f42c1" },
    { key: "cacheEntries", label: "Cache API entries", colour: "#fd7e14" },
    { key: "localStorageKeys", label: "localStorage keys", colour: "#20c997" },
  ];

  // ----- helpers ------------------------------------------------------------

  /**
   * Create an element with attributes and children. Text is added with
   * append(), never innerHTML, so names typed by users cannot inject HTML.
   */
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "style") Object.assign(node.style, value);
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child !== undefined && child !== null && child !== false) {
        node.append(child);
      }
    }
    return node;
  }

  /**
   * Format a number of bytes as B, KB, MB or GB.
   */
  function bytes(value) {
    if (value === null || value === undefined) return "n/a";
    const units = ["B", "KB", "MB", "GB"];
    let size = value;
    let unit = 0;
    while (Math.abs(size) >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit++;
    }
    return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  /**
   * Format a number of seconds as "45s" or "2m 05s".
   */
  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "n/a";
    const whole = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(whole / 60);
    const rest = String(whole % 60).padStart(2, "0");
    return minutes ? `${minutes}m ${rest}s` : `${whole}s`;
  }

  /**
   * Add a point to a chart's history, dropping the oldest when it is full.
   */
  function pushPoint(points, point) {
    points.push(point);
    if (points.length > MAX_POINTS) points.shift();
  }

  /**
   * Estimate the bytes used by a Storage object (2 bytes per character).
   */
  function storageBytes(store) {
    let total = 0;
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      total += (key.length + (store.getItem(key) || "").length) * 2;
    }
    return total;
  }

  /**
   * Show a message under the buttons.
   */
  function setMessage(text) {
    document.getElementById("lab-message").textContent = text;
  }

  /**
   * Show whether the server is answering.
   */
  function setConnection(ok) {
    const badge = document.getElementById("lab-connection");
    badge.textContent = ok ? "Live" : "Server not responding";
    badge.className = `badge ${ok ? "text-bg-success" : "text-bg-danger"}`;
  }

  /**
   * POST JSON to the lab, including the CSRF token Flask-WTF requires.
   */
  async function post(url, body = {}) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  /**
   * Replace the contents of a table with new headings and rows.
   */
  function table(id, headings, rows, emptyText) {
    const target = document.getElementById(id);
    const head = el(
      "thead",
      {},
      el(
        "tr",
        {},
        headings.map((h) => el("th", { scope: "col" }, h)),
      ),
    );
    const body = el(
      "tbody",
      {},
      rows.length
        ? rows.map((cells) =>
            el(
              "tr",
              {},
              cells.map((cell) => el("td", {}, cell)),
            ),
          )
        : el(
            "tr",
            {},
            el(
              "td",
              { colspan: headings.length, class: "text-body-secondary" },
              emptyText,
            ),
          ),
    );
    target.replaceChildren(head, body);
  }

  // ----- client measurements ------------------------------------------------

  /**
   * True for files that belong in a PWA cache: /static/ files and the
   * offline page. Anything else is a page or live data.
   */
  function isStaticAsset(url) {
    return url.startsWith("/static/") || url === "/offline.html";
  }

  /**
   * Measure this site's browser storage. localStorage, the Cache API and
   * service workers are shared by every tab, so measuring them here covers
   * all tabs at once.
   */
  async function measureClient() {
    const result = {
      localStorageKeys: localStorage.length,
      localStorageBytes: storageBytes(localStorage),
      caches: [],
      cacheEntries: 0,
      usage: null,
      quota: null,
      workers: 0,
      workerState: "unsupported",
    };
    if ("caches" in window) {
      for (const name of await caches.keys()) {
        const requests = await (await caches.open(name)).keys();
        const urls = requests.map((request) => {
          const url = new URL(request.url);
          return url.origin === location.origin
            ? url.pathname + url.search
            : url.href;
        });
        result.caches.push({
          name,
          entries: urls.length,
          dynamic: urls.filter((url) => !isStaticAsset(url)),
        });
        result.cacheEntries += urls.length;
      }
    }
    if (navigator.storage && navigator.storage.estimate) {
      const estimate = await navigator.storage.estimate();
      result.usage = estimate.usage;
      result.quota = estimate.quota;
    }
    if (navigator.serviceWorker) {
      const registrations = await navigator.serviceWorker.getRegistrations();
      result.workers = registrations.length;
      const worker = registrations
        .map((r) => r.active || r.waiting || r.installing)
        .find(Boolean);
      result.workerState = worker ? worker.state : "none";
    }
    return result;
  }

  /**
   * Explain why there are no caches yet, based on the service worker's state.
   * Service workers need a secure context (HTTPS or localhost) and are blocked
   * in private windows and VS Code's Simple Browser.
   */
  function noCacheHint() {
    if (!("caches" in window) || client.workerState === "unsupported") {
      return (
        "This browser window does not allow service workers. Open the app " +
        "in a normal Chrome or Edge window (not a private window or VS " +
        "Code's Simple Browser)."
      );
    }
    if (client.workerState === "none") {
      return "No service worker is registered yet. Reload a menu tab.";
    }
    if (client.workerState === "installing") {
      return "The service worker is installing (downloading its files).";
    }
    if (client.workerState === "redundant") {
      return (
        "The service worker failed to install. Check the menu tab's " +
        "DevTools Console, then reload it."
      );
    }
    return (
      `The service worker is ${client.workerState} but has no cache. ` +
      "Click Clear browser storage, then reload a menu tab."
    );
  }

  // ----- charts -------------------------------------------------------------

  /**
   * Draw a line chart on a canvas. Each series is one line; the y-axis
   * scales to the largest value shown.
   */
  function drawChart(canvas, points, series, format) {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = { left: 70, right: 10, top: 10, bottom: 18 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    let max = 0;
    for (const point of points) {
      for (const s of series) max = Math.max(max, point[s.key] || 0);
    }
    max = max > 0 ? max * 1.15 : 1;

    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (plotHeight * i) / 4;
      ctx.strokeStyle = "#dee2e6";
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.fillStyle = "#6c757d";
      ctx.fillText(format(max * (1 - i / 4)), pad.left - 6, y);
    }
    ctx.textAlign = "left";
    ctx.fillText(
      `last ${duration(MAX_POINTS * (POLL_MS / 1000))}`,
      pad.left,
      height - 6,
    );

    const step = plotWidth / (MAX_POINTS - 1);
    for (const s of series) {
      ctx.strokeStyle = s.colour;
      ctx.lineWidth = 2;
      ctx.beginPath();
      points.forEach((point, index) => {
        const x = pad.left + index * step;
        const y =
          pad.top + plotHeight - ((point[s.key] || 0) / max) * plotHeight;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
  }

  /**
   * Show the colour key and latest value for each line of a chart.
   */
  function legend(id, series, latest, format) {
    document.getElementById(id).replaceChildren(
      ...series.map((s) =>
        el(
          "span",
          {},
          el("span", {
            class: "lab-swatch",
            style: { backgroundColor: s.colour },
          }),
          `${s.label}: ${latest ? format(latest[s.key] || 0) : "n/a"}`,
        ),
      ),
    );
  }

  // ----- rendering ----------------------------------------------------------

  /**
   * Build one "key number" card.
   */
  function kpi(title, value, note) {
    return el(
      "div",
      { class: "col-6 col-md-4 col-xl-2" },
      el(
        "div",
        { class: "card h-100" },
        el(
          "div",
          { class: "card-body py-2" },
          el("div", { class: "small text-body-secondary" }, title),
          el("div", { class: "lab-kpi-value" }, value),
          el("div", { class: "small text-body-secondary" }, note),
        ),
      ),
    );
  }

  /**
   * Draw the row of key numbers at the top of the dashboard.
   */
  function renderKpis(stats) {
    const domTotal = stats.tabs.reduce((sum, tab) => sum + tab.dom, 0);
    const store = stats.store.module
      ? `in ${stats.store.module}.SESSIONS`
      : "SESSIONS not found";
    document
      .getElementById("kpis")
      .replaceChildren(
        kpi(
          "Python heap",
          bytes(stats.server.traced),
          `peak ${bytes(stats.server.peak)}`,
        ),
        kpi(
          "Allocated by your code",
          bytes(stats.server.attributed),
          "see table below",
        ),
        kpi(
          "Process memory (RSS)",
          bytes(stats.server.rss),
          "as seen by the OS",
        ),
        kpi("Server sessions", String(stats.store.count), store),
        kpi("Open tabs", String(stats.tabs.length), `${domTotal} DOM nodes`),
        kpi(
          "Browser storage",
          client ? bytes(client.usage) : "n/a",
          client ? `${client.cacheEntries} cached responses` : "",
        ),
      );
  }

  /**
   * Work out the client memory scorecard checks (README Steps 8, 9 and 10).
   * They are calculated here because only the browser can see its storage.
   */
  function clientChecks(stats) {
    const group = "Client memory";
    const checks = [];
    const realTabs = stats.tabs;
    const mature = realTabs.filter((tab) => tab.age >= MATURE_TAB_SECONDS);
    if (!mature.length) {
      checks.push({
        group,
        title: "The DOM stays a steady size",
        status: "waiting",
        detail: `Keep a menu tab open for ${MATURE_TAB_SECONDS} seconds.`,
      });
    } else {
      const worst = mature.reduce((a, b) =>
        b.dom_growth > a.dom_growth ? b : a,
      );
      checks.push({
        group,
        title: "The DOM stays a steady size",
        status: worst.dom_growth <= DOM_GROWTH_LIMIT ? "pass" : "fail",
        detail: `Tab ${worst.tab} grew by ${worst.dom_growth} nodes in ${duration(worst.age)}.`,
      });
    }
    if (!client) return checks;

    let status = "waiting";
    if (client.localStorageKeys > LOCAL_STORAGE_KEY_LIMIT) status = "fail";
    else if (realTabs.length) status = "pass";
    checks.push({
      group,
      title: "localStorage holds only what is needed",
      status,
      detail: `${client.localStorageKeys} keys using ${bytes(client.localStorageBytes)}.`,
    });

    const dynamic = client.caches.flatMap((cache) => cache.dynamic);
    if (!client.caches.length) {
      checks.push({
        group,
        title: "Service worker caches static files only",
        status: "waiting",
        detail: noCacheHint(),
      });
    } else {
      checks.push({
        group,
        title: "Service worker caches static files only",
        status: dynamic.length ? "fail" : "pass",
        detail: dynamic.length
          ? `${dynamic.length} pages or live responses cached, e.g. ${dynamic[dynamic.length - 1]}`
          : "Only /static/ files and the offline page are cached.",
      });
    }

    let cacheStatus = "waiting";
    if (client.caches.length > 1) cacheStatus = "fail";
    else if (client.caches.length === 1) cacheStatus = "pass";
    checks.push({
      group,
      title: "Old caches are deleted",
      status: cacheStatus,
      detail: client.caches.length
        ? `Caches: ${client.caches.map((cache) => cache.name).join(", ")}`
        : noCacheHint(),
    });
    return checks;
  }

  /**
   * Draw the scorecard, grouped into server memory, client memory and
   * session management.
   */
  function renderChecks(stats) {
    const all = [...stats.checks, ...clientChecks(stats)];
    const order = ["Server memory", "Client memory", "Session management"];
    const symbol = { pass: "✔", fail: "✘", waiting: "…" };
    document.getElementById("checks").replaceChildren(
      ...order.map((group) => {
        const items = all.filter((check) => check.group === group);
        const passed = items.filter((check) => check.status === "pass").length;
        return el(
          "div",
          { class: "col-lg-4" },
          el("h3", { class: "h6" }, `${group} (${passed}/${items.length})`),
          items.map((check) =>
            el(
              "div",
              { class: `lab-check ${check.status} card mb-2` },
              el(
                "div",
                { class: "card-body py-2 small" },
                el("strong", {}, `${symbol[check.status]} ${check.title}`),
                el("div", { class: "text-body-secondary" }, check.detail),
              ),
            ),
          ),
        );
      }),
    );
  }

  /**
   * Draw one tile for each server session.
   */
  function renderSessions(stats) {
    const timeout = stats.store.idle_timeout;
    const realCount = stats.sessions.filter((s) => !s.simulated).length;
    const simulatedCount = stats.sessions.length - realCount;
    const totalBytes = stats.sessions.reduce((sum, s) => sum + s.bytes, 0);
    document.getElementById("session-count").textContent = String(
      stats.store.count,
    );
    document.getElementById("session-summary").textContent =
      `${realCount} real and ${simulatedCount} simulated sessions ` +
      `using ${bytes(totalBytes)} in total. ` +
      (timeout
        ? `Idle sessions expire after ${duration(timeout)}.`
        : "Sessions never expire.");

    document.getElementById("sessions").replaceChildren(
      ...stats.sessions.map((s) => {
        const idleShare =
          timeout && s.idle !== null ? Math.min(1, s.idle / timeout) : 0;
        const classes = [
          "lab-tile",
          s.you ? "you" : "",
          s.simulated ? "simulated" : "",
        ].join(" ");
        const strength = s.sequential
          ? el("span", { class: "lab-bad" }, `~${s.bits} bits (counting!)`)
          : el(
              "span",
              { class: s.bits >= 128 ? "lab-good" : "lab-bad" },
              `~${s.bits} bits`,
            );
        return el(
          "div",
          { class: classes, style: { opacity: String(1 - idleShare * 0.7) } },
          el(
            "div",
            { class: "d-flex flex-wrap align-items-center gap-1 mb-1" },
            el("span", {
              class: "lab-swatch",
              style: { backgroundColor: `#${s.id}` },
            }),
            el("span", { class: "lab-tile-id" }, s.id),
            s.you && el("span", { class: "badge text-bg-primary" }, "You"),
            s.simulated &&
              el("span", { class: "badge text-bg-secondary" }, "Simulated"),
            s.regenerated &&
              el(
                "span",
                { class: "badge text-bg-success" },
                "New ID on sign in",
              ),
            s.fixation &&
              el(
                "span",
                { class: "badge text-bg-danger" },
                "Same ID after sign in",
              ),
          ),
          el("div", {}, el("strong", {}, s.user || "Guest")),
          el("div", {}, `Size: ${bytes(s.bytes)}`),
          el("div", {}, "ID strength: ", strength),
          el("div", {}, `Age ${duration(s.age)} · Idle ${duration(s.idle)}`),
          timeout &&
            el(
              "div",
              { class: "lab-idle-bar", title: "Idle time until expiry" },
              el("div", { style: { width: `${idleShare * 100}%` } }),
            ),
          el(
            "div",
            {},
            `Tabs: ${s.tabs.length ? s.tabs.join(", ") : "none open"}`,
          ),
          el(
            "div",
            { class: "text-body-secondary lab-mono" },
            `keys: ${s.keys.join(", ")}`,
          ),
        );
      }),
    );
  }

  /**
   * Fill the server tables: memory by line, module-level data and caches.
   */
  function renderServerTables(stats) {
    table(
      "attribution",
      ["Line", "Code", "Live memory", "Blocks"],
      stats.attribution.map((row) => [
        el("span", { class: "lab-mono" }, `${row.file}:${row.line}`),
        el("code", {}, row.code),
        bytes(row.bytes),
        String(row.blocks),
      ]),
      "Nothing attributed yet.",
    );
    table(
      "containers",
      ["Name", "Type", "Items", "Size"],
      stats.containers.map((row) => [
        el("span", { class: "lab-mono" }, row.name),
        row.maxlen ? `${row.type} (max ${row.maxlen})` : row.type,
        String(row.items),
        bytes(row.bytes),
      ]),
      "No module-level containers.",
    );
    table(
      "caches",
      ["Function", "Hits", "Misses", "Entries"],
      stats.caches.map((row) => [
        el("span", { class: "lab-mono" }, row.name),
        String(row.hits),
        String(row.misses),
        `${row.currsize}/${row.maxsize === null ? "∞" : row.maxsize}`,
      ]),
      "No caches found.",
    );
  }

  /**
   * Show a tick or cross with a label, coloured green or red.
   */
  function flag(ok, text) {
    return el(
      "span",
      { class: ok ? "lab-good" : "lab-bad" },
      `${ok ? "✔" : "✘"} ${text}`,
    );
  }

  /**
   * Fill the browser-side tables: open tabs, cookies and storage.
   */
  function renderClientTables(stats) {
    table(
      "tabs",
      ["Tab", "Session", "Page", "DOM nodes", "sessionStorage", "JS heap"],
      stats.tabs.map((tab) => [
        el("span", { class: "lab-mono" }, tab.tab),
        tab.session
          ? el(
              "span",
              { class: "lab-mono" },
              el("span", {
                class: "lab-swatch",
                style: { backgroundColor: `#${tab.session}` },
              }),
              tab.session,
            )
          : "none",
        tab.path,
        `${tab.dom} (${tab.dom_growth >= 0 ? "+" : ""}${tab.dom_growth})`,
        bytes(tab.session_storage),
        tab.heap ? bytes(tab.heap) : "n/a",
      ]),
      "No app tabs open. Use the buttons above.",
    );

    table(
      "cookies",
      ["Cookie", "Flags"],
      stats.cookies.map((cookie) => [
        el(
          "div",
          {},
          el("strong", { class: "lab-mono" }, cookie.name),
          el("div", { class: "text-body-secondary" }, cookie.kind || ""),
        ),
        el(
          "div",
          {},
          flag(cookie.httponly, "HttpOnly"),
          el("br"),
          flag(cookie.secure, "Secure"),
          el("br"),
          flag(
            Boolean(cookie.samesite),
            `SameSite${cookie.samesite ? "=" + cookie.samesite : ""}`,
          ),
          el("br"),
          flag(
            Boolean(cookie.lifetime),
            cookie.lifetime ? `Expires (${cookie.lifetime})` : "Expires",
          ),
          cookie.deleted_at &&
            el(
              "div",
              { class: "text-body-secondary" },
              "Deleted by the server",
            ),
        ),
      ]),
      "No cookies seen yet.",
    );

    const storage = document.getElementById("storage");
    if (!client) {
      storage.replaceChildren(el("p", { class: "small" }, "Measuring…"));
      return;
    }
    storage.replaceChildren(
      el(
        "ul",
        { class: "small mb-2" },
        el(
          "li",
          {},
          `localStorage: ${client.localStorageKeys} keys, ${bytes(client.localStorageBytes)}`,
        ),
        el(
          "li",
          {},
          `Service workers registered: ${client.workers} (${client.workerState})`,
        ),
        el(
          "li",
          {},
          `Browser estimate of storage used: ${bytes(client.usage)} of ${bytes(client.quota)}`,
        ),
      ),
      el("p", { class: "small text-body-secondary mb-1" }, "Cache API caches:"),
      client.caches.length
        ? el(
            "ul",
            { class: "small" },
            client.caches.map((cache) =>
              el(
                "li",
                {},
                el("span", { class: "lab-mono" }, cache.name),
                `: ${cache.entries} entries (${cache.dynamic.length} not static)`,
              ),
            ),
          )
        : el("p", { class: "small" }, "None."),
    );
  }

  // ----- polling ------------------------------------------------------------

  /**
   * Fetch the latest measurements, redraw everything, then schedule the next
   * update. setTimeout (rather than setInterval) waits for each update to
   * finish, so slow updates never pile up.
   */
  async function tick() {
    try {
      const response = await fetch("/lab/stats", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const stats = await response.json();
      if (!client || Date.now() - lastClientMeasure >= CLIENT_MS) {
        client = await measureClient();
        lastClientMeasure = Date.now();
      }
      pushPoint(serverPoints, {
        traced: stats.server.traced,
        attributed: stats.server.attributed,
      });
      pushPoint(clientPoints, {
        dom: stats.tabs.reduce((sum, tab) => sum + tab.dom, 0),
        cacheEntries: client.cacheEntries,
        localStorageKeys: client.localStorageKeys,
      });

      const count = (v) => String(Math.round(v));
      drawChart(
        document.getElementById("server-chart"),
        serverPoints,
        SERVER_SERIES,
        bytes,
      );
      drawChart(
        document.getElementById("client-chart"),
        clientPoints,
        CLIENT_SERIES,
        count,
      );
      legend("server-legend", SERVER_SERIES, serverPoints.at(-1), bytes);
      legend("client-legend", CLIENT_SERIES, clientPoints.at(-1), count);
      renderKpis(stats);
      renderChecks(stats);
      renderSessions(stats);
      renderServerTables(stats);
      renderClientTables(stats);
      setConnection(true);
    } catch (error) {
      setConnection(false);
    } finally {
      setTimeout(tick, POLL_MS);
    }
  }

  // ----- controls -----------------------------------------------------------

  const SAME_SESSION_MESSAGE =
    "Opened in this browser, so it shares your cookies and joins YOUR session " +
    "(look for a new tab ID on your tile). For a new session, open " +
    `${location.origin}/ in a private/incognito window or another browser.`;

  document.getElementById("open-tab").addEventListener("click", () => {
    window.open("/", "_blank", "noopener");
    setMessage(SAME_SESSION_MESSAGE);
  });

  document.getElementById("open-window").addEventListener("click", () => {
    window.open("/", "_blank", "popup,noopener,width=1100,height=800");
    setMessage(SAME_SESSION_MESSAGE);
  });

  document.querySelectorAll("[data-customers]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      setMessage(`Adding ${button.dataset.customers} customers…`);
      try {
        const result = await post("/lab/simulate", {
          customers: Number(button.dataset.customers),
        });
        setMessage(
          `${result.customers} customers ordered and left, creating ` +
            `${result.sessions_created} new sessions. Does your code keep them?`,
        );
      } catch (error) {
        setMessage(`Adding customers failed: ${error.message}`);
      } finally {
        button.disabled = false;
      }
    });
  });

  document
    .getElementById("reset-server")
    .addEventListener("click", async () => {
      try {
        await post("/lab/reset");
        serverPoints.length = 0;
        setMessage(
          "Server sessions, caches and lab history cleared. Reload app tabs to start again.",
        );
      } catch (error) {
        setMessage(`Reset failed: ${error.message}`);
      }
    });

  document
    .getElementById("clear-browser")
    .addEventListener("click", async () => {
      localStorage.clear();
      if ("caches" in window) {
        for (const name of await caches.keys()) await caches.delete(name);
      }
      if (navigator.serviceWorker) {
        const registrations = await navigator.serviceWorker.getRegistrations();
        for (const registration of registrations)
          await registration.unregister();
      }
      clientPoints.length = 0;
      client = null;
      setMessage(
        "localStorage, Cache API caches and service workers cleared. Reload app tabs to reinstall the service worker.",
      );
    });

  tick();
})();
