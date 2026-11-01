/**
 * Memory & Session Lab probe.
 *
 * The lab adds this script to every page of the student app. Every 2 seconds
 * it measures this tab and sends the numbers to /lab/probe. It also shows a
 * badge in the bottom-right corner with this tab's session and tab ID, so you
 * can see which tabs share a session.
 */
(() => {
  "use strict";

  const TAB_KEY = "labTabId";
  const INTERVAL_MS = 2000;

  /**
   * Make a short random ID for this tab.
   */
  function newTabId() {
    if (window.crypto && crypto.randomUUID) {
      return crypto.randomUUID().slice(0, 8);
    }
    return Math.random().toString(16).slice(2, 10);
  }

  // sessionStorage belongs to one tab, so each tab keeps its own ID even
  // though every tab in the browser shares the same cookies.
  let tabId = sessionStorage.getItem(TAB_KEY);
  if (!tabId) {
    tabId = newTabId();
    sessionStorage.setItem(TAB_KEY, tabId);
  }

  // "Duplicate tab" copies sessionStorage, so ask other tabs whether this ID
  // is already in use and pick a new one if it is.
  if ("BroadcastChannel" in window) {
    const channel = new BroadcastChannel("memory-lab-tabs");
    const loadedAt = performance.timeOrigin;
    channel.onmessage = (event) => {
      const { id, time } = event.data || {};
      if (id !== tabId) return;
      if (time > loadedAt) {
        channel.postMessage({ id: tabId, time: loadedAt });
      } else if (time < loadedAt) {
        tabId = newTabId();
        sessionStorage.setItem(TAB_KEY, tabId);
      }
    };
    channel.postMessage({ id: tabId, time: loadedAt });
  }

  /**
   * Estimate the bytes used by a Storage object (JavaScript stores text as
   * UTF-16, which uses 2 bytes per character).
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
   * Choose black or white text so it is readable on a background colour.
   */
  function readableText(hex) {
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return r * 0.299 + g * 0.587 + b * 0.114 > 150 ? "#000" : "#fff";
  }

  const badge = document.createElement("a");
  badge.href = "/lab";
  badge.target = "_blank";
  badge.rel = "noopener";
  badge.className = "lab-badge";
  badge.title = "Memory & Session Lab: this page's session and browser tab";
  badge.textContent = `Tab ${tabId}`;
  document.body.appendChild(badge);

  /**
   * Send this tab's measurements to the lab and update the badge.
   */
  async function report() {
    const body = {
      tab: tabId,
      path: location.pathname + location.search,
      pageLoad: Math.round(performance.timeOrigin),
      dom: document.getElementsByTagName("*").length,
      sessionStorage: storageBytes(sessionStorage),
      heap: performance.memory ? performance.memory.usedJSHeapSize : null,
    };
    try {
      const response = await fetch("/lab/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
        credentials: "same-origin",
      });
      const info = await response.json();
      const who = info.user ? ` (${info.user})` : "";
      badge.textContent = `Session ${info.session || "none"}${who} · Tab ${tabId}`;
      if (info.session) {
        badge.style.backgroundColor = `#${info.session}`;
        badge.style.color = readableText(info.session);
      } else {
        badge.style.backgroundColor = "";
        badge.style.color = "";
      }
    } catch (error) {
      badge.textContent = `Lab offline · Tab ${tabId}`;
    }
  }

  report();
  setInterval(report, INTERVAL_MS);
})();
