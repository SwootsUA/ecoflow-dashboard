const el = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const num = (v, digits = 0) =>
  v == null ? "—" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const STALE_AFTER = 180; // seconds without a reading before the page says so
const HOLDING_OVER = 24 * 60; // minutes: an estimate longer than a day means the level is not really moving
const STEP = { 1: 600, 6: 3600, 24: 6 * 3600, 168: 86400, 720: 5 * 86400 }; // seconds between x ticks
const CHARTED = ["battery_level", "input_power", "output_power", "ac_input_voltage", "inverter_temperature"];
const TAIL_MAX = 1800; // live readings kept for the charts: half an hour at one a second
const FLOOR = 18; // watts: the station reports 0 for anything smaller (the lowest non-zero value it has ever sent)

let hours = 24;
let last = {};
let stored = null; // the last /api/history answer
const tail = []; // live readings received since the page opened, oldest first
const charts = {};
const view = { from: 0, to: 0 }; // the time window every chart shows, in ms

// the session ended (or was never there): back to the login page
async function getJson(url) {
  const response = await fetch(url);
  if (response.status === 401) location.href = "/login";
  if (!response.ok) throw new Error(response.status);
  return response.json();
}

function duration(minutes) {
  const h = Math.floor(minutes / 60);
  return h ? `${h} h ${minutes % 60} min` : `${minutes} min`;
}

function ago(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

// runs every second, so the age counts up between readings
function showAge() {
  if (!last.ts) return;
  const age = Math.max(0, Date.now() / 1000 - last.ts);
  const at = new Date(last.ts * 1000).toLocaleTimeString("en-GB");
  const stale = age > STALE_AFTER;
  const live = Boolean(last.live) && age < 5; // "live" only while readings are actually arriving
  el("updated").classList.toggle("stale", stale);
  el("updated").classList.toggle("live", live);
  if (stale) el("updated").textContent = `No reading since ${at} (${ago(age)})`;
  else el("updated").textContent = live ? `Live, ${at}` : `Updated ${at}, ${ago(age)}`;
}

function showLatest(d) {
  const first = !last.ts;
  const fresh = d.ts !== last.ts;
  last = d;
  showAge();

  const gridOn = d.ac_input_voltage > 50 || d.ac_input_power > 0;
  const net = (d.input_power ?? 0) - (d.output_power ?? 0);
  const emptying = !gridOn || net < 0;
  const minutes = emptying ? d.remaining_time_discharging : net > 0 ? d.remaining_time_charging : null;
  let left = ["Time left", "—", "holding level"];
  if (minutes > 0 && minutes <= HOLDING_OVER)
    left = emptying ? ["Until empty", duration(minutes), "at this load"] : ["Until full", duration(minutes), "at this rate"];

  const stats = [
    ["Battery", `${num(d.battery_level, 1)}%`, `limits ${num(d.battery_charge_limit_min)}–${num(d.battery_charge_limit_max)}%`],
    ["Grid", gridOn ? "On" : "Off", gridOn ? `${num(d.ac_input_voltage)} V` : "running on battery", gridOn ? "" : "off"],
    ["Output", `${num(d.output_power)} W`, `input ${num(d.input_power)} W`],
    left,
  ];
  el("stats").innerHTML = stats
    .map(
      ([label, value, sub, tone]) =>
        `<div class="card"><h2>${label}</h2><div class="stat-value ${tone ?? ""}">${value}</div><div class="stat-sub">${sub}</div></div>`,
    )
    .join("");
  el("inverter-note").textContent =
    d.inverter_temperature == null ? "" : `now ${num(d.inverter_temperature)}, line estimated between the station's 5° steps`;
  showAll();

  if (!fresh) return;
  if (d.live) addLive(d);
  else if (!first) loadHistory(); // a new stored reading is a new point on every chart
}

function showAll() {
  if (!el("all").closest("details").open) return;
  const want = el("filter").value.trim().toLowerCase();
  el("all").innerHTML = Object.keys(last)
    .sort()
    .filter((key) => key.includes(want))
    .map((key) => `<tr><td>${esc(key)}</td><td>${esc(last[key] ?? "—")}</td></tr>`)
    .join("");
}

// A visible tab asks once a second. Asking is also the signal: the Pi holds its Bluetooth
// link to the station only while these requests keep coming, so a hidden tab stops asking.
let asking = false;
let nextAsk = null;

async function ask() {
  if (asking || document.hidden) return;
  asking = true;
  try {
    const d = await getJson("/api/live");
    if (d.ts) showLatest(d);
    else el("updated").textContent = "No readings yet";
  } catch {
    el("updated").textContent = "Page server not reachable";
    el("updated").classList.add("stale");
  }
  asking = false;
  clearTimeout(nextAsk);
  nextAsk = setTimeout(ask, 1000);
}

// One live reading: remember it, and move the right edge of every chart without redrawing them.
function addLive(d) {
  // back after a pause (the tab was hidden): the old live stretch would hide the readings stored since
  const resumed = tail.length && d.ts - tail.at(-1).ts > 120;
  if (resumed) tail.length = 0;
  tail.push(Object.fromEntries(["ts", ...CHARTED].map((name) => [name, d[name] ?? null])));
  if (tail.length > TAIL_MAX) tail.shift();
  if (resumed) return loadHistory();
  if (!stored) return;
  view.to = Date.now();
  view.from = view.to - hours * 3600 * 1000;
  for (const chart of Object.values(charts)) {
    for (const set of chart.data.datasets) {
      if (set.shape) set.data = set.shape(points(set.column)); // reshaped lines are rebuilt, not appended to
      else if (set.column) set.data.push({ x: d.ts * 1000, y: d[set.column] ?? null });
    }
    chart.options.scales.x.min = view.from;
    chart.options.scales.x.max = view.to;
    chart.update("none");
  }
}

// The points of one value: stored readings, with the live stretch (one a second) in place of
// the stored ones it overlaps. A hole in the readings stays a hole in the line.
function points(name) {
  const column = stored.columns.indexOf(name);
  const step = Math.max(stored.bucket, 60);
  const liveFrom = tail[0]?.ts ?? Infinity;
  const liveTo = tail.at(-1)?.ts ?? -Infinity;
  const out = [];
  let before = null;
  const add = (ts, y) => {
    if (before != null && ts - before > 2.5 * step) out.push({ x: before * 1000 + 1, y: null });
    out.push({ x: ts * 1000, y });
    before = ts;
  };
  for (const row of stored.rows) if (row[0] < liveFrom) add(row[0], column < 0 ? null : row[column]);
  for (const reading of tail) add(reading.ts, reading[name]);
  // only with a live stretch: without one, every stored row is already in
  if (tail.length) for (const row of stored.rows) if (row[0] > liveTo) add(row[0], column < 0 ? null : row[column]);
  return out;
}

// The station reports the inverter temperature only in 5-degree bands. The real value crosses
// a band edge at the moment the reading changes, so the line is drawn through those crossings
// (halfway between the two readings, in time and in value) instead of as steps.
function throughCrossings(stepped) {
  const out = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    out.push(run[0]);
    let edge = null;
    for (let i = 1; i < run.length; i++) {
      if (run[i].y === run[i - 1].y) continue;
      const next = { x: (run[i].x + run[i - 1].x) / 2, y: (run[i].y + run[i - 1].y) / 2 };
      // it turned around inside one band: show that band's own value in the middle of the stay
      if (edge && edge.y === next.y) out.push({ x: (edge.x + next.x) / 2, y: run[i - 1].y });
      out.push(next);
      edge = next;
    }
    if (run.length > 1) out.push(run.at(-1));
    run = [];
  };
  for (const point of stepped) {
    if (point.y == null) {
      flush();
      out.push(point); // a hole in the readings stays a hole
    } else run.push(point);
  }
  flush();
  return out;
}

function draw(id, series, y = {}) {
  charts[id]?.destroy();
  charts[id] = new Chart(el(id), {
    type: "line",
    data: {
      datasets: series.map(({ label, color, column, data, shape, extra }) => ({
        label,
        column,
        shape,
        data: data ?? (column ? (shape ? shape(points(column)) : points(column)) : []),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 12,
        spanGaps: false,
        ...extra,
      })),
    },
    options: {
      parsing: false,
      animation: false,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: series.length > 1, labels: { color: css("--fg"), boxWidth: 12, boxHeight: 2 } },
        tooltip: {
          filter: (item) => Boolean(item.dataset.column), // readings only, not the marker lines
          callbacks: {
            title: (items) =>
              new Date(items[0].parsed.x).toLocaleString("en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }),
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: view.from,
          max: view.to,
          grid: { color: css("--bg3") },
          ticks: {
            color: css("--fg2"),
            maxRotation: 0,
            callback: (ms) =>
              hours <= 24
                ? new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
                : new Date(ms).toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit" }),
          },
          // ticks on round local times (every 6 hours, every midnight) instead of offsets from "now"
          afterBuildTicks: (axis) => {
            const step = STEP[hours] * 1000;
            const midnight = new Date(view.from).setHours(0, 0, 0, 0);
            axis.ticks = [];
            for (let t = midnight; t <= view.to; t += step) if (t >= view.from) axis.ticks.push({ value: t });
          },
        },
        y: { ticks: { color: css("--fg2") }, grid: { color: css("--bg3") }, ...y },
      },
    },
  });
  return charts[id];
}

async function loadHistory() {
  el("csv").href = `/api/readings.csv?hours=${hours}`;
  try {
    stored = await getJson(`/api/history?hours=${hours}`);
  } catch {
    return;
  }
  view.to = Date.now();
  view.from = view.to - hours * 3600 * 1000;

  draw("chart-level", [{ label: "Battery %", color: css("--accent"), column: "battery_level" }], { min: 0, max: 100 });
  draw("chart-grid", [{ label: "Grid V", color: css("--accent"), column: "ac_input_voltage" }], { min: 0 });
  draw("chart-inverter", [
    { label: "Inverter °C", color: css("--accent"), column: "inverter_temperature", shape: throughCrossings },
  ]);

  // While the station only passes grid power through, in and out are one line: that stretch is drawn grey.
  // Both lines are built from the same readings, so index i is the same moment in each.
  const grey = css("--fg2");
  const same = (sets, i) => {
    const out = sets[1].data[i]?.y;
    const input = sets[2].data[i]?.y;
    return out != null && input != null && Math.abs(out - input) < 1;
  };
  const passthrough = {
    segment: {
      borderColor: (ctx) => {
        const sets = ctx.chart.data.datasets;
        return same(sets, ctx.p0DataIndex) && same(sets, ctx.p1DataIndex) ? grey : undefined;
      },
    },
  };
  const power = draw(
    "chart-power",
    [
      { label: "In = out", color: grey }, // no points of its own; it puts grey in the legend
      { label: "Out", color: css("--series2"), column: "output_power", extra: passthrough },
      { label: "In", color: css("--accent"), column: "input_power", extra: passthrough },
      {
        // one dashed line across the whole time axis, whatever window is shown
        label: `reads 0 below ${FLOOR} W`,
        color: grey,
        data: [{ x: 0, y: FLOOR }, { x: 4102444800000, y: FLOOR }],
        extra: { borderDash: [6, 4], borderWidth: 1, pointHitRadius: 0 },
      },
    ],
    { min: 0 },
  );
  const mean = (set) => {
    const ys = set.data.map((p) => p.y).filter((y) => y != null);
    return ys.length ? Math.round(ys.reduce((a, b) => a + b, 0) / ys.length) : null;
  };
  el("power-note").textContent = `average in ${num(mean(power.data.datasets[2]))}, out ${num(mean(power.data.datasets[1]))}`;
}

for (const button of document.querySelectorAll("[data-hours]")) {
  button.addEventListener("click", () => {
    hours = Number(button.dataset.hours);
    document.querySelector("[data-hours].active")?.classList.remove("active");
    button.classList.add("active");
    loadHistory();
  });
}
el("filter").addEventListener("input", showAll);
el("all").closest("details").addEventListener("toggle", showAll);
document.addEventListener("visibilitychange", ask);

loadHistory();
ask();
setInterval(showAge, 1000);
// live points pile up in the charts; rebuilding now and then keeps them to stored readings plus the last half hour
setInterval(() => last.live && !document.hidden && loadHistory(), 300_000);
