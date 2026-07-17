"use strict";

const WINDOW_META = {
  "5h": { label: "5-hour", cssVar: "--series-5h" },
  "7d": { label: "7-day", cssVar: "--series-7d" },
  month: { label: "Monthly", cssVar: "--series-month" },
};
const WINDOW_ORDER = Object.keys(WINDOW_META);
const PROVIDERS = ["claude", "codex"];
const PROVIDER_LABEL = { claude: "Claude", codex: "Codex" };
const REFRESH_MS = 60000;

function windowMeta(w) {
  return WINDOW_META[w] || { label: w, cssVar: "--series-5h" };
}

// Flat, fixed-order list of every (provider, window) series present in the
// data — the combined chart draws one line per entry. Order is stable
// (provider then window), so each series keeps its own color across refreshes
// and range changes.
function seriesList(latest, history) {
  const out = [];
  for (const provider of PROVIDERS) {
    const latestWins = (latest.latest || {})[provider] || {};
    const histWins = (history || {})[provider] || {};
    for (const w of WINDOW_ORDER) {
      if (w in latestWins || w in histWins) {
        out.push({
          provider: provider,
          window: w,
          label: PROVIDER_LABEL[provider] + " " + windowMeta(w).label,
          cssVar: windowMeta(w).cssVar,
        });
      }
    }
  }
  return out;
}

let combinedChart = null;
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
    `<div class="tile-label">${escapeHTML(label)}</div>` +
    `<div class="tile-value">${value}</div>` +
    `<div class="tile-sub">${sub}</div>`;
}

// Build the uPlot data matrix [xs, ...ySeries] by unioning the sample
// timestamps of every series. Providers share one ts per sample cycle, so
// their points align exactly on the shared x-axis.
function buildAligned(history, series, latest) {
  const stamps = new Set();
  for (const s of series) {
    for (const [ts] of (history[s.provider] || {})[s.window] || []) stamps.add(ts);
  }
  const xs = Array.from(stamps).sort();
  const index = new Map(xs.map((ts, i) => [ts, i]));
  const xVals = xs.map((ts) => Date.parse(ts) / 1000);
  const seriesYs = series.map((s) => {
    const ys = new Array(xs.length).fill(null);
    for (const [ts, pct] of (history[s.provider] || {})[s.window] || []) {
      ys[index.get(ts)] = pct;
    }
    return ys;
  });
  // Stepped(align:1) gives the newest sample zero drawn width, so without
  // help the chart visually lags one sample and stops at the last tick.
  // Extend each series whose most recent sample succeeded out to "now";
  // series whose latest sample errored keep their honest gap.
  const nowSec = Math.floor(Date.now() / 1000);
  const extendY = series.map((s) => {
    const info = ((latest.latest || {})[s.provider] || {})[s.window];
    return info && info.used_percent != null ? info.used_percent : null;
  });
  if (xVals.length && nowSec > xVals[xVals.length - 1] && extendY.some((y) => y != null)) {
    xVals.push(nowSec);
    seriesYs.forEach((ys, i) => ys.push(extendY[i]));
  }
  return [xVals, ...seriesYs];
}

function makeChart(el, data, series) {
  const axisStyle = {
    stroke: cssColor("--muted"),
    grid: { stroke: cssColor("--grid"), width: 1 },
    ticks: { stroke: cssColor("--axis"), width: 1 },
  };
  const opts = {
    width: el.clientWidth || 600,
    height: 300,
    scales: { y: { range: [0, 100] } },
    axes: [
      { ...axisStyle },
      { ...axisStyle, values: (u, vals) => vals.map((v) => v + "%") },
    ],
    series: [
      {},
      ...series.map((s) => ({
        label: s.label,
        stroke: cssColor(s.cssVar),
        width: 2,
        spanGaps: false,
        points: { show: false },
        paths: uPlot.paths.stepped({ align: 1 }),
      })),
    ],
  };
  return new uPlot(opts, data, el);
}

function renderChart(history, series, latest) {
  const el = document.getElementById("chart-combined");
  const data = buildAligned(history, series, latest);
  if (combinedChart) {
    combinedChart.destroy();
    combinedChart = null;
  }
  el.textContent = "";
  if (series.length && data[0].length) {
    combinedChart = makeChart(el, data, series);
  } else {
    el.textContent = "no samples in this range";
  }
}

function renderTiles(series, latest) {
  const tiles = document.getElementById("tiles-all");
  tiles.textContent = "";
  if (series.length === 0) {
    tiles.innerHTML = '<div class="tile"><div class="tile-label">no data</div>' +
      '<div class="tile-value">–</div><div class="tile-sub">no samples yet</div></div>';
    return;
  }
  for (const s of series) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.id = `tile-${s.provider}-${s.window}`;
    tiles.appendChild(tile);
    renderTile(tile, s.label, ((latest.latest || {})[s.provider] || {})[s.window]);
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
  const series = seriesList(latest, hist.history);
  renderTiles(series, latest);
  renderChart(hist.history, series, latest);
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
  if (combinedChart) {
    combinedChart.setSize({
      width: combinedChart.root.parentElement.clientWidth || 600,
      height: 300,
    });
  }
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);

refresh();
setInterval(refresh, REFRESH_MS);
