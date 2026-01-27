import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import fs from "fs/promises";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = process.env.PORT ?? 8080;
const ECOFLOW_BASE_URL = process.env.ECOFLOW_BASE_URL ?? "https://api-e.ecoflow.com";
const ENABLE_LOGGING = (process.env.ENABLE_LOGGING ?? "1") !== "0";
const LOG_DIR = process.env.LOG_DIR ?? path.join(__dirname, "logs");
const LOG_MIN_INTERVAL_MS = Math.max(0, Number(process.env.LOG_MIN_INTERVAL_MS ?? 10_000));
const lastLoggedAtBySn = new Map();

function safeSn(sn) {
  return String(sn).replace(/[^a-zA-Z0-9_-]/g, "_");
}

function dayKeyFromMs(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function logFilePath(sn, dayKey) {
  return path.join(LOG_DIR, `${safeSn(sn)}-${dayKey}.jsonl`);
}

async function ensureLogDir() {
  await fs.mkdir(LOG_DIR, { recursive: true });
}

function pick(obj, keys) {
  if (!obj || typeof obj !== "object") return undefined;
  for (const k of keys) {
    if (Object.prototype.hasOwnProperty.call(obj, k) && obj[k] != null) return obj[k];
  }
}

function extractMainDatapoints(data) {
  return {
    soc: pick(data, ["pd.soc", "soc", "bms_bmsStatus.soc"]),
    watts_in: pick(data, ["pd.wattsInSum", "pd.inputWatts", "mppt.inWatts"]),
    watts_out: pick(data, ["pd.wattsOutSum", "pd.outputWatts"]),
    remain_min: pick(data, ["pd.remainTime"]),
    batt_vol_mv: pick(data, ["bms_bmsStatus.vol"]),
    batt_temp_c: pick(data, ["bms_bmsStatus.temp"]),
    inv_temp_c: pick(data, ["inv.outTemp"]),
    mppt_temp_c: pick(data, ["mppt.mpptTemp"]),
    wifi_rssi: pick(data, ["pd.wifiRssi"]),
  };
}

async function maybeAppendLog({ sn, data }) {
  if (!ENABLE_LOGGING) return;

  const now = Date.now();
  const prev = lastLoggedAtBySn.get(sn) ?? 0;
  if (LOG_MIN_INTERVAL_MS && now - prev < LOG_MIN_INTERVAL_MS) return;
  lastLoggedAtBySn.set(sn, now);

  await ensureLogDir();

  const record = {
    ts: now,
    iso: new Date(now).toISOString(),
    sn,
    ...extractMainDatapoints(data),
  };

  const file = logFilePath(sn, dayKeyFromMs(now));
  await fs.appendFile(file, JSON.stringify(record) + "\n", "utf8");
}

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(express.static(path.join(__dirname, "public"), { extensions: ["html"] }));

function getKeys(req) {
  const accessKey = req.get("x-ecoflow-accesskey") || process.env.ECOFLOW_ACCESS_KEY;
  const secretKey = req.get("x-ecoflow-secretkey") || process.env.ECOFLOW_SECRET_KEY;

  if (!accessKey || !secretKey) {
    const err = new Error(
      "Missing EcoFlow keys. Set ECOFLOW_ACCESS_KEY/ECOFLOW_SECRET_KEY env vars or pass X-Ecoflow-AccessKey/X-Ecoflow-SecretKey headers.",
    );
    err.status = 400;
    throw err;
  }
  return { accessKey, secretKey };
}

function createNonce() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

function createTimestamp() {
  return String(Date.now());
}

function flattenData(data, prefix = "") {
  const out = {};
  if (data === null || data === undefined) return out;
  if (typeof data !== "object") return out;

  if (Array.isArray(data)) {
    for (let i = 0; i < data.length; i++) {
      const newKey = prefix === "" ? String(i) : `${prefix}[${i}]`;
      const v = data[i];
      if (v && typeof v === "object") Object.assign(out, flattenData(v, newKey));
      else out[newKey] = v;
    }
    return out;
  }

  for (const [k, v] of Object.entries(data)) {
    const newKey = prefix === "" ? k : `${prefix}.${k}`;
    if (v && typeof v === "object") Object.assign(out, flattenData(v, newKey));
    else out[newKey] = v;
  }
  return out;
}

function buildSignatureBase({ accessKey, nonce, timestamp, bodyOrParams }) {
  const flat = flattenData(bodyOrParams ?? {});
  const keys = Object.keys(flat).sort((a, b) => a.localeCompare(b));

  const usp = new URLSearchParams();
  for (const k of keys) usp.append(k, String(flat[k]));
  const query = decodeURIComponent(usp.toString().replace(/\+/g, " "));

  const prefix = query ? `${query}&` : "";
  return `${prefix}accessKey=${accessKey}&nonce=${nonce}&timestamp=${timestamp}`;
}

function signHmacSha256Hex(secretKey, message) {
  return crypto.createHmac("sha256", secretKey).update(message, "utf8").digest("hex");
}

async function ecoflowRequest({ method, path, queryString = "", accessKey, secretKey, body = null, signData = null }) {
  const nonce = createNonce();
  const timestamp = createTimestamp();

  const base = buildSignatureBase({
    accessKey,
    nonce,
    timestamp,
    bodyOrParams: signData ?? body ?? {},
  });

  const signature = signHmacSha256Hex(secretKey, base);

  const url = `${ECOFLOW_BASE_URL}${path}${queryString}`;
  const headers = {
    "Content-Type": "application/json;charset=UTF-8",
    accessKey,
    nonce,
    timestamp,
    sign: signature,
  };

  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await resp.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text };
  }

  if (!resp.ok) {
    const err = new Error(`EcoFlow HTTP ${resp.status}`);
    err.status = 502;
    err.details = { url, method, body, response: json };
    throw err;
  }

  return json;
}

app.get("/api/devices", async (req, res, next) => {
  try {
    const { accessKey, secretKey } = getKeys(req);
    const json = await ecoflowRequest({
      method: "GET",
      path: "/iot-open/sign/device/list",
      accessKey,
      secretKey,
      signData: {},
    });
    res.json(json);
  } catch (e) {
    next(e);
  }
});

app.get("/api/status/:sn", async (req, res, next) => {
  try {
    const { accessKey, secretKey } = getKeys(req);
    const sn = req.params.sn;

    const json = await ecoflowRequest({
      method: "GET",
      path: "/iot-open/sign/device/quota/all",
      queryString: `?sn=${encodeURIComponent(sn)}`,
      accessKey,
      secretKey,
      signData: {},
    });

    if (json && json.data) {
      maybeAppendLog({ sn, data: json.data }).catch(() => {});
    }

    res.json(json);
  } catch (e) {
    next(e);
  }
});

app.post("/api/quota/:sn", async (req, res, next) => {
  try {
    const { accessKey, secretKey } = getKeys(req);
    const sn = req.params.sn;

    const quotas = req.body?.quotas;
    if (!Array.isArray(quotas) || quotas.length === 0) {
      const err = new Error('Body must be like: {"quotas":["pd.soc","pd.wattsInSum"]}');
      err.status = 400;
      throw err;
    }

    const body = { sn, params: { quotas } };

    const json = await ecoflowRequest({
      method: "POST",
      path: "/iot-open/sign/device/quota",
      accessKey,
      secretKey,
      body,
      signData: body,
    });

    res.json(json);
  } catch (e) {
    next(e);
  }
});

function parseRangeMs(range) {
  const m = String(range || "").match(/^(\d+)([mhd])$/i);
  if (!m) return 86_400_000;
  const n = Number(m[1]);
  return m[2] === "m" ? n * 60_000 : m[2] === "h" ? n * 3_600_000 : n * 86_400_000;
}

app.get("/api/log/:sn", async (req, res, next) => {
  try {
    const sn = req.params.sn;
    const now = Date.now();
    const rangeMs = parseRangeMs(req.query.range);
    const since = now - rangeMs;

    await ensureLogDir();
    const dayKey = dayKeyFromMs(now);
    const file = logFilePath(sn, dayKey);

    let rows = [];
    try {
      const text = await fs.readFile(file, "utf8");
      rows = text
        .split("\n")
        .filter(Boolean)
        .map(JSON.parse)
        .filter((r) => r.ts >= since);
    } catch {}

    res.json({ sn, count: rows.length, data: rows });
  } catch (e) {
    next(e);
  }
});

app.use((err, req, res, next) => {
  const status = err.status || 500;
  res.status(status).json({
    error: err.message || "Unknown error",
    details: err.details || undefined,
  });
});

app.listen(PORT, () => {
  console.log(`EcoFlow dashboard: http://localhost:${PORT}`);
});
