import crypto from "crypto";

const ECOFLOW_BASE_URL = process.env.ECOFLOW_BASE_URL ?? "https://api-e.ecoflow.com";

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

export async function ecoflowRequest({ method, path, queryString = "", accessKey, secretKey, body = null, signData = null }) {
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

  // (3) Ask upstream not to cache + force fetch to not use cache
  const headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "Cache-Control": "no-cache",
    Pragma: "no-cache",
    accessKey,
    nonce,
    timestamp,
    sign: signature,
  };

  const resp = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
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
