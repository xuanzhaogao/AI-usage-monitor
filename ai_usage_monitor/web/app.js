"use strict";

const WINDOW_META = {
  "5h": { label: "5-hour", cssVar: "--series-5h" },
  "7d": { label: "7-day", cssVar: "--series-7d" },
  month: { label: "Monthly", cssVar: "--series-month" },
};
const WINDOW_ORDER = Object.keys(WINDOW_META);
const PROVIDERS = ["claude", "codex"];
const REFRESH_MS = 60000;

function windowsFor(provider, latest, history) {
  const present = new Set([
    ...Object.keys((latest.latest || {})[provider] || {}),
    ...Object.keys((history || {})[provider] || {}),
  ]);
  return WINDOW_ORDER.filter((w) => present.has(w));
}

function windowMeta(w) {
  return WINDOW_META[w] || { label: w, cssVar: "--series-5h" };
}

const charts = {};
let currentHours = 24;

function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(url + " returned " + resp.status);
  return resp.json();
}

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function formatReset(iso) {
  if (!iso) return "reset time unknown";
  const ms = Date.parse(iso) - Date.now();
  if (Number.isNaN(ms)) return "reset time unknown";
  if (ms <= 0) return "resets now";
  const totalMinutes = Math.round(ms / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `resets in ${days}d ${hours}h`;
  if (hours > 0) return `resets in ${hours}h ${minutes}m`;
  return `resets in ${minutes}m`;
}

function renderTile(el, label, info) {
  const value = info && info.used_percent != null
    ? Math.round(info.used_percent) + "%"
    : "–";
  const sub = !info ? "no data"
    : info.error ? escapeHTML(info.error)
    : formatReset(info.resets_at);
  el.innerHTML =
    `<div class="tile-label">${label}</div>` +
    `<div class="tile-value">${value}</div>` +
    `<div class="tile-sub">${sub}</div>`;
}

function buildAligned(providerHistory, windows) {
  const stamps = new Set();
  for (const w of windows) {
    for (const [ts] of providerHistory[w] || []) stamps.add(ts);
  }
  const xs = Array.from(stamps).sort();
  const index = new Map(xs.map((ts, i) => [ts, i]));
  const data = [xs.map((ts) => Date.parse(ts) / 1000)];
  for (const w of windows) {
    const ys = new Array(xs.length).fill(null);
    for (const [ts, pct] of providerHistory[w] || []) {
      ys[index.get(ts)] = pct;
    }
    data.push(ys);
  }
  return data;
}

function makeChart(el, data, windows) {
  const axisStyle = {
    stroke: cssColor("--muted"),
    grid: { stroke: cssColor("--grid"), width: 1 },
    ticks: { stroke: cssColor("--axis"), width: 1 },
  };
  const opts = {
    width: el.clientWidth || 600,
    height: 260,
    scales: { y: { range: [0, 100] } },
    axes: [
      { ...axisStyle },
      { ...axisStyle, values: (u, vals) => vals.map((v) => v + "%") },
    ],
    series: [
      {},
      ...windows.map((w) => ({
        label: windowMeta(w).label,
        stroke: cssColor(windowMeta(w).cssVar),
        width: 2,
        spanGaps: false,
        points: { show: false },
        paths: uPlot.paths.stepped({ align: 1 }),
      })),
    ],
  };
  return new uPlot(opts, data, el);
}

function renderChart(provider, history, windows) {
  const el = document.getElementById("chart-" + provider);
  const data = buildAligned(history[provider] || {}, windows);
  if (charts[provider]) {
    charts[provider].destroy();
    delete charts[provider];
  }
  el.textContent = "";
  if (data[0].length) {
    charts[provider] = makeChart(el, data, windows);
  } else {
    el.textContent = "no samples in this range";
  }
}

function renderBanner(latest) {
  const banner = document.getElementById("stale-banner");
  if (latest.age_minutes == null) {
    banner.hidden = false;
    banner.textContent =
      "⚠ No samples yet — run “python3 -m ai_usage_monitor sample”, " +
      "then “install-agent” for continuous sampling.";
  } else if (latest.age_minutes > 30) {
    banner.hidden = false;
    banner.textContent =
      `⚠ Last sample ${Math.round(latest.age_minutes)} minutes ago — ` +
      "the sampler may not be running (run install-agent).";
  } else {
    banner.hidden = true;
  }
}

async function refresh() {
  const banner = document.getElementById("stale-banner");
  let latest, hist;
  try {
    [latest, hist] = await Promise.all([
      fetchJSON("/api/latest"),
      fetchJSON("/api/history?hours=" + currentHours),
    ]);
  } catch (err) {
    banner.hidden = false;
    banner.textContent = "⚠ Dashboard update failed: " + err.message;
    return;
  }
  renderBanner(latest);
  for (const provider of PROVIDERS) {
    const wins = windowsFor(provider, latest, hist.history);
    const tiles = document.getElementById("tiles-" + provider);
    tiles.textContent = "";
    for (const w of wins) {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.id = `tile-${provider}-${w}`;
      tiles.appendChild(tile);
      renderTile(tile, windowMeta(w).label, (latest.latest[provider] || {})[w]);
    }
    if (wins.length === 0) {
      tiles.innerHTML = '<div class="tile"><div class="tile-label">no data</div>' +
        '<div class="tile-value">–</div><div class="tile-sub">no samples yet</div></div>';
    }
    renderChart(provider, hist.history, wins);
  }
}

document.getElementById("range-picker").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-hours]");
  if (!button) return;
  currentHours = Number(button.dataset.hours);
  for (const b of document.querySelectorAll("#range-picker button")) {
    b.classList.toggle("active", b === button);
  }
  refresh();
});

window.addEventListener("resize", () => {
  for (const provider of PROVIDERS) {
    const chart = charts[provider];
    if (chart) {
      chart.setSize({
        width: chart.root.parentElement.clientWidth || 600,
        height: 260,
      });
    }
  }
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);

refresh();
setInterval(refresh, REFRESH_MS);
