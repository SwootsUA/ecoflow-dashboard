const STORAGE_KEY = "ecoflowDashboardConfig.v1";

const state = {
  config: loadConfig(),
  devices: [],
  selectedSn: "",
  lastStatus: null,
  timer: null,
  charts: {},
  logData: [],
  csvUrl: null,
};

const el = {
  pageSettings: document.getElementById("pageSettings"),
  pageDashboard: document.getElementById("pageDashboard"),
  navDashboard: document.getElementById("navDashboard"),
  navSettings: document.getElementById("navSettings"),

  accessKey: document.getElementById("accessKey"),
  secretKey: document.getElementById("secretKey"),
  refreshMs: document.getElementById("refreshMs"),
  saveSettings: document.getElementById("saveSettings"),
  clearSettings: document.getElementById("clearSettings"),

  loadDevices: document.getElementById("loadDevices"),
  refreshNow: document.getElementById("refreshNow"),
  autoRefresh: document.getElementById("autoRefresh"),
  deviceSelect: document.getElementById("deviceSelect"),

  statusLine: document.getElementById("statusLine"),
  lastUpdated: document.getElementById("lastUpdated"),
  summaryCards: document.getElementById("summaryCards"),

  filterInput: document.getElementById("filterInput"),
  kvBody: document.getElementById("kvBody"),
  rawJson: document.getElementById("rawJson"),
  countLine: document.getElementById("countLine"),

  errorBox: document.getElementById("errorBox"),

  powerBreakdown: document.getElementById("powerBreakdown"),
  batteryDetails: document.getElementById("batteryDetails"),
  cellVoltages: document.getElementById("cellVoltages"),

  pageGraphs: document.getElementById("pageGraphs"),
  navGraphs: document.getElementById("navGraphs"),

  graphsRange: document.getElementById("graphsRange"),
  graphsRefresh: document.getElementById("graphsRefresh"),
  graphsDownload: document.getElementById("graphsDownload"),
  graphsError: document.getElementById("graphsError"),

  chartSoc: document.getElementById("chartSoc"),
  chartPower: document.getElementById("chartPower"),
  chartBattV: document.getElementById("chartBattV"),
  chartBattTemp: document.getElementById("chartBattTemp"),
};

function loadConfig() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw)
    return {
      accessKey: "",
      secretKey: "",
      refreshMs: 2000,
      autoRefresh: true,
      lastSn: "",
    };
  try {
    const parsed = JSON.parse(raw);
    return {
      accessKey: parsed.accessKey ?? "",
      secretKey: parsed.secretKey ?? "",
      refreshMs: Number(parsed.refreshMs ?? 2000),
      autoRefresh: Boolean(parsed.autoRefresh ?? true),
      lastSn: parsed.lastSn ?? "",
    };
  } catch {
    return {
      accessKey: "",
      secretKey: "",
      refreshMs: 2000,
      autoRefresh: true,
      lastSn: "",
    };
  }
}

function saveConfig() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.config));
}

function setRoute(route) {
  location.hash = route;
}

function showPage(which) {
  const isSettings = which === "settings";
  el.pageSettings.classList.toggle("hidden", !isSettings);
  el.pageDashboard.classList.toggle("hidden", isSettings);

  el.navSettings.classList.toggle("active", isSettings);
  el.navDashboard.classList.toggle("active", !isSettings);
}

function route() {
  const h = location.hash || "#/dashboard";
  if (h === "#/graphs") {
    showPage("graphs");
    refreshGraphs();
    return;
  }
  showPage(h === "#/settings" ? "settings" : "dashboard");
}

function renderGraphs(rows) {
  if (!window.Chart) return;

  const labels = rows.map((r) => new Date(r.ts).toLocaleTimeString());
  const soc = rows.map((r) => r.soc ?? null);
  const wattsIn = rows.map((r) => r.watts_in ?? null);
  const wattsOut = rows.map((r) => r.watts_out ?? null);
  const battV = rows.map((r) => (r.batt_vol_mv ? r.batt_vol_mv / 1000 : null));
  const battTemp = rows.map((r) => r.batt_temp_c ?? null);

  new Chart(el.chartSoc, { type: "line", data: { labels, datasets: [{ label: "SOC %", data: soc }] } });
  new Chart(el.chartPower, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "In W", data: wattsIn },
        { label: "Out W", data: wattsOut },
      ],
    },
  });
  new Chart(el.chartBattV, { type: "line", data: { labels, datasets: [{ label: "Battery V", data: battV }] } });
  new Chart(el.chartBattTemp, { type: "line", data: { labels, datasets: [{ label: "Battery °C", data: battTemp }] } });
}

async function refreshGraphs() {
  if (!state.selectedSn) return;
  const range = el.graphsRange.value;
  const res = await fetch(`/api/log/${state.selectedSn}?range=${range}`);
  const json = await res.json();
  state.logData = json.data || [];
  renderGraphs(state.logData);
}

window.addEventListener("hashchange", route);

function headersWithKeys() {
  const headers = {};
  if (state.config.accessKey) headers["X-Ecoflow-AccessKey"] = state.config.accessKey;
  if (state.config.secretKey) headers["X-Ecoflow-SecretKey"] = state.config.secretKey;
  return headers;
}

async function apiGet(url) {
  const resp = await fetch(url, { headers: headersWithKeys() });
  const json = await resp.json();
  if (!resp.ok) throw new Error(json?.error || `Request failed: ${resp.status}`);
  return json;
}

function setStatus(text) {
  el.statusLine.textContent = text;
}

function showError(msg) {
  el.errorBox.textContent = msg;
  el.errorBox.classList.remove("hidden");
}

function clearError() {
  el.errorBox.classList.add("hidden");
  el.errorBox.textContent = "";
}

function pick(obj, keys) {
  for (const k of keys) {
    if (!k) continue;
    if (Object.prototype.hasOwnProperty.call(obj, k) && obj[k] !== null && obj[k] !== undefined) return obj[k];
  }
  return undefined;
}

function fmtWatts(v) {
  if (v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${Math.round(n)} W`;
}

function fmtPercent(v) {
  if (v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${Math.round(n)}%`;
}

function fmtRemainTimeSeconds(v) {
  if (v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);

  // EcoFlow "remainTime" fields are minutes (not seconds).
  const minutes = Math.abs(n);
  const hours = minutes / 60;
  const mode = n >= 0 ? "charge" : "discharge";

  // Example: 5939 => 99.0 h (not 99 min)
  return `${hours.toFixed(1)} h (${Math.round(minutes)} min) • ${mode}`;
}

function fmtVoltsFromMilli(v) {
  if (v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${(n / 1000).toFixed(3)} V`;
}

function fmtFreq(v) {
  if (v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${n} Hz`;
}

function fmtBool(v) {
  if (v === undefined) return "—";
  return Number(v) ? "Yes" : "No";
}

function addBarRow(label, valueWatts, maxWatts) {
  const v = Number(valueWatts);
  const m = Math.max(1, Number(maxWatts) || 1);
  const pct = Math.max(0, Math.min(100, (v / m) * 100));
  return `
    <div class="barRow">
      <div class="barLabel">${escapeHtml(label)}</div>
      <div class="barTrack"><div class="barFill" style="width:${pct.toFixed(1)}%"></div></div>
      <div class="barValue">${escapeHtml(fmtWatts(v))}</div>
    </div>
  `;
}

function renderSummaryCards(data, deviceMeta) {
  const soc = pick(data, ["pd.soc", "soc", "pd.bpPowerSoc", "bms_emsStatus.lcdShowSoc", "bms_bmsStatus.soc"]);

  const wattsIn = pick(data, ["pd.wattsInSum", "pd.inputWatts", "inv.inputWatts", "mppt.inWatts"]);
  const wattsOut = pick(data, ["pd.wattsOutSum", "pd.outputWatts", "inv.outputWatts"]);

  const remain = pick(data, ["pd.remainTime", "bms_emsStatus.chgRemainTime", "bms_emsStatus.dsgRemainTime"]);

  const acInV = pick(data, ["inv.acInVol"]);
  const acOutV = pick(data, ["inv.invOutVol"]);
  const acOutHz = pick(data, ["inv.invOutFreq"]);

  const battV = pick(data, ["bms_bmsStatus.vol"]);
  const battTemp = pick(data, ["bms_bmsStatus.temp"]);
  const invTemp = pick(data, ["inv.outTemp"]);
  const mpptTemp = pick(data, ["mppt.mpptTemp"]);

  const soh = pick(data, ["bms_bmsStatus.soh", "bms_bmsInfo.soh"]);
  const cycles = pick(data, ["bms_bmsStatus.cycles", "bms_bmsInfo.bsmCycles"]);

  const maxChargeSoc = pick(data, ["bms_emsStatus.maxChargeSoc"]);
  const minDsgSoc = pick(data, ["bms_emsStatus.minDsgSoc"]);

  const wifiRssi = pick(data, ["pd.wifiRssi"]);
  const acEnabled = pick(data, ["pd.acEnabled", "inv.cfgAcEnabled"]);

  const online = deviceMeta?.online ?? deviceMeta?.Online;
  const productName = deviceMeta?.productName ?? deviceMeta?.product_name ?? deviceMeta?.product ?? "Device";

  const items = [
    { k: "SOC", v: fmtPercent(soc), s: productName },
    { k: "Input (total)", v: fmtWatts(wattsIn), s: "watts in" },
    { k: "Output (total)", v: fmtWatts(wattsOut), s: "watts out" },
    {
      k: "Remain time",
      v: fmtRemainTimeSeconds(remain),
      s: "EcoFlow minutes → hours",
    },

    { k: "AC enabled", v: fmtBool(acEnabled), s: "state/config" },
    {
      k: "AC in voltage",
      v: acInV === undefined ? "—" : fmtVoltsFromMilli(acInV),
      s: "inv.acInVol",
    },
    {
      k: "AC out voltage",
      v: acOutV === undefined ? "—" : fmtVoltsFromMilli(acOutV),
      s: "inv.invOutVol",
    },
    { k: "AC out freq", v: fmtFreq(acOutHz), s: "inv.invOutFreq" },

    {
      k: "Battery voltage",
      v: battV === undefined ? "—" : fmtVoltsFromMilli(battV),
      s: "bms_bmsStatus.vol",
    },
    {
      k: "Battery temp",
      v: battTemp === undefined ? "—" : `${Math.round(Number(battTemp))} °C`,
      s: "bms_bmsStatus.temp",
    },
    {
      k: "Inverter temp",
      v: invTemp === undefined ? "—" : `${Math.round(Number(invTemp))} °C`,
      s: "inv.outTemp",
    },
    {
      k: "MPPT temp",
      v: mpptTemp === undefined ? "—" : `${Math.round(Number(mpptTemp))} °C`,
      s: "mppt.mpptTemp",
    },

    {
      k: "SOH",
      v: soh === undefined ? "—" : `${Math.round(Number(soh))}%`,
      s: "battery health",
    },
    {
      k: "Cycles",
      v: cycles === undefined ? "—" : String(cycles),
      s: "charge cycles",
    },
    {
      k: "Max charge SOC",
      v: maxChargeSoc === undefined ? "—" : `${maxChargeSoc}%`,
      s: "limit",
    },
    {
      k: "Min discharge SOC",
      v: minDsgSoc === undefined ? "—" : `${minDsgSoc}%`,
      s: "limit",
    },

    {
      k: "Wi-Fi RSSI",
      v: wifiRssi === undefined ? "—" : String(wifiRssi),
      s: "pd.wifiRssi",
    },
    {
      k: "Online",
      v: online === undefined ? "—" : Number(online) ? "Yes" : "No",
      s: "device list",
    },
    { k: "SN", v: state.selectedSn || "—", s: "" },
  ];

  el.summaryCards.innerHTML = items
    .map(
      (x) => `
      <div class="metric">
        <div class="k">${escapeHtml(x.k)}</div>
        <div class="v">${escapeHtml(String(x.v))}</div>
        <div class="s">${escapeHtml(String(x.s || ""))}</div>
      </div>
    `,
    )
    .join("");
}

function escapeHtml(s) {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderKeyValueTable(data, filterText) {
  const entries = Object.entries(data || {}).sort((a, b) => a[0].localeCompare(b[0]));
  const f = (filterText || "").trim().toLowerCase();

  const filtered = f
    ? entries.filter(([k, v]) => k.toLowerCase().includes(f) || String(v).toLowerCase().includes(f))
    : entries;

  el.countLine.textContent = `${filtered.length} / ${entries.length} keys`;

  el.kvBody.innerHTML = filtered
    .map(([k, v]) => {
      const vv = typeof v === "object" ? JSON.stringify(v) : String(v);
      return `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(vv)}</td></tr>`;
    })
    .join("");
}

function renderPowerBreakdown(data) {
  if (!el.powerBreakdown) return;

  const totalOut = Number(pick(data, ["pd.wattsOutSum", "pd.outputWatts", "inv.outputWatts"])) || 0;
  const totalIn = Number(pick(data, ["pd.wattsInSum", "pd.inputWatts", "inv.inputWatts", "mppt.inWatts"])) || 0;
  const max = Math.max(totalIn, totalOut, 1);

  const parts = [
    ["AC output", pick(data, ["inv.outputWatts"])],
    ["Device output (pd)", pick(data, ["pd.outputWatts"])],
    ["USB1", pick(data, ["pd.usb1Watts"])],
    ["USB2", pick(data, ["pd.usb2Watts"])],
    ["QC USB1", pick(data, ["pd.qcUsb1Watts"])],
    ["QC USB2", pick(data, ["pd.qcUsb2Watts"])],
    ["Type-C1", pick(data, ["pd.typec1Watts"])],
    ["Type-C2", pick(data, ["pd.typec2Watts"])],
    ["Car outlet", pick(data, ["pd.carWatts"])],
    ["Wireless", pick(data, ["pd.wireWatts"])],

    ["AC input", pick(data, ["inv.inputWatts"])],
    ["Device input (pd)", pick(data, ["pd.inputWatts"])],
    ["Solar (pd)", pick(data, ["pd.chgSunPower"])],
    ["MPPT input", pick(data, ["mppt.inWatts"])],
  ];

  const rows = parts.map(([label, v]) => [label, Number(v) || 0]).filter(([, v]) => v !== 0);

  let html = "";
  html += addBarRow("Total input", totalIn, max);
  html += addBarRow("Total output", totalOut, max);

  if (rows.length) {
    html += `<div style="height:6px"></div>`;
    for (const [label, v] of rows) html += addBarRow(label, v, max);
  } else {
    html += `<div class="meta">No per-port watt fields reported (or all are 0).</div>`;
  }

  el.powerBreakdown.innerHTML = html;
}

function renderBatteryDetails(data) {
  if (!el.batteryDetails) return;

  const items = [
    ["Design capacity", pick(data, ["bms_bmsStatus.designCap"])],
    ["Full capacity", pick(data, ["bms_bmsStatus.fullCap"])],
    ["Remaining capacity", pick(data, ["bms_bmsStatus.remainCap"])],
    [
      "Battery voltage",
      pick(data, ["bms_bmsStatus.vol"]) !== undefined ? fmtVoltsFromMilli(pick(data, ["bms_bmsStatus.vol"])) : "—",
    ],
    [
      "Battery temp",
      pick(data, ["bms_bmsStatus.temp"]) !== undefined
        ? `${Math.round(Number(pick(data, ["bms_bmsStatus.temp"])))} °C`
        : "—",
    ],
    [
      "SOH",
      pick(data, ["bms_bmsStatus.soh", "bms_bmsInfo.soh"]) !== undefined
        ? `${Math.round(Number(pick(data, ["bms_bmsStatus.soh", "bms_bmsInfo.soh"])))}%`
        : "—",
    ],
    ["Cycles", pick(data, ["bms_bmsStatus.cycles", "bms_bmsInfo.bsmCycles"])],
    [
      "Max charge SOC",
      pick(data, ["bms_emsStatus.maxChargeSoc"]) !== undefined ? `${pick(data, ["bms_emsStatus.maxChargeSoc"])}%` : "—",
    ],
    [
      "Min discharge SOC",
      pick(data, ["bms_emsStatus.minDsgSoc"]) !== undefined ? `${pick(data, ["bms_emsStatus.minDsgSoc"])}%` : "—",
    ],
    ["AC enabled", fmtBool(pick(data, ["pd.acEnabled", "inv.cfgAcEnabled"]))],
    ["Beep", fmtBool(pick(data, ["mppt.beepState", "pd.beepMode"]))],
    ["Wi-Fi RSSI", pick(data, ["pd.wifiRssi"])],
  ];

  el.batteryDetails.innerHTML = items
    .map(([k, v]) => {
      const vv = v === undefined ? "—" : String(v);
      return `<div class="kvMini"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(vv)}</div></div>`;
    })
    .join("");
}

function renderCellVoltages(data) {
  if (!el.cellVoltages) return;

  const cells = pick(data, ["bms_bmsStatus.cellVol"]);
  if (!Array.isArray(cells) || cells.length === 0) {
    el.cellVoltages.innerHTML = `<div class="meta">No cell voltage array reported.</div>`;
    return;
  }

  const mv = cells.map((x) => Number(x)).filter((x) => Number.isFinite(x));
  const min = Math.min(...mv);
  const max = Math.max(...mv);
  const span = Math.max(1, max - min);

  el.cellVoltages.innerHTML = mv
    .map((v, i) => {
      const pct = ((v - min) / span) * 100;
      return `
        <div class="cellCard">
          <div class="cellTop">
            <div class="k">Cell ${i + 1}</div>
            <div class="v">${escapeHtml((v / 1000).toFixed(3))} V</div>
          </div>
          <div class="cellBar"><div class="cellFill" style="width:${pct.toFixed(1)}%"></div></div>
        </div>
      `;
    })
    .join("");
}

function normalizeDeviceList(apiResponse) {
  const data = apiResponse?.data;

  let list = [];
  if (Array.isArray(data)) list = data;
  else if (Array.isArray(data?.devices)) list = data.devices;
  else if (Array.isArray(data?.Devices)) list = data.Devices;

  return list
    .map((d) => ({
      sn: d.sn ?? d.SN ?? d.deviceSn ?? d.deviceSN,
      deviceName: d.deviceName ?? d.name ?? d.device_name ?? d.device,
      productName: d.productName ?? d.product ?? d.product_name,
      online: d.online ?? d.Online,
    }))
    .filter((d) => d.sn);
}

async function loadDevices() {
  clearError();
  setStatus("Loading device list…");
  const json = await apiGet("/api/devices");

  if (json?.code !== "0" && json?.code !== 0 && json?.Code !== 0) {
    throw new Error(`EcoFlow error: ${json?.message || json?.Message || "Unknown"}`);
  }

  state.devices = normalizeDeviceList(json);

  el.deviceSelect.innerHTML =
    `<option value="">— Select a device —</option>` +
    state.devices
      .map((d) => {
        const label = `${d.deviceName || "EcoFlow"} • ${d.productName || ""} • ${d.sn} • ${Number(d.online) ? "online" : "offline"}`;
        return `<option value="${escapeHtml(d.sn)}">${escapeHtml(label)}</option>`;
      })
      .join("");

  const preferred =
    state.config.lastSn && state.devices.some((d) => d.sn === state.config.lastSn)
      ? state.config.lastSn
      : (state.devices[0]?.sn ?? "");

  if (preferred) {
    el.deviceSelect.value = preferred;
    state.selectedSn = preferred;
    state.config.lastSn = preferred;
    saveConfig();
  }

  setStatus(`Loaded ${state.devices.length} device(s).`);
}

async function fetchStatus() {
  clearError();
  if (!state.selectedSn) {
    setStatus("Select a device first.");
    return;
  }

  setStatus("Fetching status…");
  const json = await apiGet(`/api/status/${encodeURIComponent(state.selectedSn)}`);

  if (json?.code !== "0" && json?.code !== 0 && json?.Code !== 0) {
    throw new Error(`EcoFlow error: ${json?.message || json?.Message || "Unknown"}`);
  }

  const data = json?.data ?? {};
  state.lastStatus = data;

  renderSummaryCards(
    data,
    state.devices.find((d) => d.sn === state.selectedSn),
  );
  renderKeyValueTable(data, el.filterInput.value);
  renderPowerBreakdown(data);
  renderBatteryDetails(data);
  renderCellVoltages(data);
  el.rawJson.textContent = JSON.stringify(json, null, 2);

  el.lastUpdated.textContent = `Last updated: ${new Date().toLocaleString()}`;
  setStatus("OK");
}

function startAutoRefresh() {
  stopAutoRefresh();
  if (!el.autoRefresh.checked) return;

  const ms = Math.max(500, Number(state.config.refreshMs) || 2000);
  state.timer = setInterval(() => {
    fetchStatus().catch((e) => {
      showError(String(e?.message || e));
      setStatus("Error");
    });
  }, ms);
}

function stopAutoRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

function bind() {
  el.accessKey.value = state.config.accessKey;
  el.secretKey.value = state.config.secretKey;
  el.refreshMs.value = String(state.config.refreshMs);

  el.graphsRefresh.addEventListener("click", refreshGraphs);
  el.graphsRange.addEventListener("change", refreshGraphs);

  el.saveSettings.addEventListener("click", () => {
    state.config.accessKey = el.accessKey.value.trim();
    state.config.secretKey = el.secretKey.value.trim();
    state.config.refreshMs = Math.max(500, Number(el.refreshMs.value) || 2000);
    saveConfig();
    setRoute("#/dashboard");
  });

  el.clearSettings.addEventListener("click", () => {
    localStorage.removeItem(STORAGE_KEY);
    state.config = loadConfig();
    el.accessKey.value = "";
    el.secretKey.value = "";
    el.refreshMs.value = "2000";
  });

  el.loadDevices.addEventListener("click", async () => {
    try {
      await loadDevices();
      if (state.selectedSn) {
        await fetchStatus();
        startAutoRefresh();
      }
    } catch (e) {
      showError(String(e?.message || e));
      setStatus("Error");
    }
  });

  el.refreshNow.addEventListener("click", async () => {
    try {
      await fetchStatus();
    } catch (e) {
      showError(String(e?.message || e));
      setStatus("Error");
    }
  });

  el.deviceSelect.addEventListener("change", async () => {
    state.selectedSn = el.deviceSelect.value;
    state.config.lastSn = state.selectedSn;
    saveConfig();
    try {
      await fetchStatus();
    } catch (e) {
      showError(String(e?.message || e));
      setStatus("Error");
    }
  });

  el.autoRefresh.checked = Boolean(state.config.autoRefresh);
  el.autoRefresh.addEventListener("change", () => {
    state.config.autoRefresh = el.autoRefresh.checked;
    saveConfig();
    startAutoRefresh();
  });

  el.filterInput.addEventListener("input", () => {
    if (!state.lastStatus) return;
    renderKeyValueTable(state.lastStatus, el.filterInput.value);
  });

  route();
}

bind();
