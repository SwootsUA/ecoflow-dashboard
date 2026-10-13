const el = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (text) => String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const num = (v, digits = 0) =>
  v == null ? "—" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const STALE_AFTER = 180; // seconds without a reading before the page says so
const NO_ESTIMATE = 5939; // minutes the station reports when it has no time estimate
const STEP = { 1: 600, 6: 3600, 24: 6 * 3600, 168: 86400, 720: 5 * 86400 }; // seconds between x ticks

let hours = 24;
let last = {};
const charts = {};

// the session ended (or was never there): back to the login page
async function getJson(url) {
  const response = await fetch(url);
  if (response.status === 401) location.href = "/login";
  if (!response.ok) throw new Error(response.status);
  return response.json();
}

function duration(minutes) {
  if (minutes == null || minutes <= 0 || minutes >= NO_ESTIMATE) return "—";
  const h = Math.floor(minutes / 60);
  return h ? `${h} h ${minutes % 60} min` : `${minutes} min`;
}

function ago(seconds) {
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function showLatest(d) {
  last = d;
  const age = Date.now() / 1000 - d.ts;
  const at = new Date(d.ts * 1000).toLocaleTimeString("en-GB");
  el("updated").classList.toggle("stale", age > STALE_AFTER);
  el("updated").textContent = age > STALE_AFTER ? `No reading since ${at} (${ago(age)})` : `Updated ${at}, ${ago(age)}`;

  const gridOn = d.ac_input_voltage > 50 || d.ac_input_power > 0;
  const net = (d.input_power ?? 0) - (d.output_power ?? 0);
  let left = ["Time left", "—", "holding level"];
  if (!gridOn || net < 0) left = ["Until empty", duration(d.remaining_time_discharging), "at this load"];
  else if (net > 0) left = ["Until full", duration(d.remaining_time_charging), "at this rate"];

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
  showAll();
}

function showAll() {
  const want = el("filter").value.trim().toLowerCase();
  el("all").innerHTML = Object.keys(last)
    .sort()
    .filter((key) => key.includes(want))
    .map((key) => `<tr><td>${esc(key)}</td><td>${esc(last[key] ?? "—")}</td></tr>`)
    .join("");
}

async function loadLatest() {
  try {
    const d = await getJson("/api/latest");
    if (d.ts) return showLatest(d);
    el("updated").textContent = "No readings yet";
  } catch {
    el("updated").textContent = "Page server not reachable";
  }
  el("updated").classList.add("stale");
}

function draw(id, series, from, to, y = {}) {
  const step = STEP[hours] * 1000;
  charts[id]?.destroy();
  charts[id] = new Chart(el(id), {
    type: "line",
    data: {
      datasets: series.map(([label, color, data]) => ({
        label,
        data,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: 0,
        pointHitRadius: 12,
        spanGaps: false,
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
          callbacks: {
            title: (items) =>
              new Date(items[0].parsed.x).toLocaleString("en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
          },
        },
      },
      scales: {
        x: {
          type: "linear",
          min: from,
          max: to,
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
            const midnight = new Date(from).setHours(0, 0, 0, 0);
            axis.ticks = [];
            for (let t = midnight; t <= to; t += step) if (t >= from) axis.ticks.push({ value: t });
          },
        },
        y: { ticks: { color: css("--fg2") }, grid: { color: css("--bg3") }, ...y },
      },
    },
  });
}

async function loadHistory() {
  el("csv").href = `/api/readings.csv?hours=${hours}`;
  let h;
  try {
    h = await getJson(`/api/history?hours=${hours}`);
  } catch {
    return;
  }
  const column = (name) => {
    const i = h.columns.indexOf(name);
    const points = [];
    let before = null;
    for (const row of h.rows) {
      // a hole in the readings stays a hole in the line
      if (before != null && row[0] - before > 2.5 * h.bucket) points.push({ x: (before + h.bucket) * 1000, y: null });
      points.push({ x: row[0] * 1000, y: i < 0 ? null : row[i] });
      before = row[0];
    }
    return points;
  };
  const mean = (points) => {
    const ys = points.map((p) => p.y).filter((y) => y != null);
    return ys.length ? Math.round(ys.reduce((a, b) => a + b, 0) / ys.length) : null;
  };
  const to = Date.now();
  const from = to - hours * 3600 * 1000;
  const input = column("input_power");
  const output = column("output_power");
  el("power-note").textContent = `average in ${num(mean(input))}, out ${num(mean(output))}`;

  draw("chart-level", [["Battery %", css("--accent"), column("battery_level")]], from, to, { min: 0, max: 100 });
  // "Out" first so it is drawn on top: on grid the two lines coincide and the load is the one to see
  draw("chart-power", [["Out", css("--series2"), output], ["In", css("--accent"), input]], from, to, { min: 0 });
  draw("chart-grid", [["Grid V", css("--accent"), column("ac_input_voltage")]], from, to, { min: 0 });
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
loadLatest();
loadHistory();
setInterval(loadLatest, 30_000);
setInterval(loadHistory, 300_000);
