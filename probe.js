// Does the station report to the cloud while the phone app is closed?
// Listens on the open-API MQTT feed and polls the HTTP API once a minute, one line per event.
// Run with the app force-closed, then open the app and compare:
//   node --env-file=.env probe.js
import fs from "fs";
import mqtt from "mqtt";
import { ecoflowRequest } from "./ecoflow.js";

const accessKey = process.env.ECOFLOW_ACCESS_KEY;
const secretKey = process.env.ECOFLOW_SECRET_KEY;
if (!accessKey || !secretKey) throw new Error("Set ECOFLOW_ACCESS_KEY and ECOFLOW_SECRET_KEY in .env");

const POLL_MS = 60_000;
const WATCH = ["pd.soc", "pd.wattsInSum", "pd.wattsOutSum", "soc", "wattsInSum", "wattsOutSum"];

const stamp = () => new Date().toTimeString().slice(0, 8);
const brief = (o) => WATCH.filter((k) => o?.[k] != null).map((k) => `${k}=${o[k]}`).join(" ");

async function api(method, path, extra = {}) {
  const res = await ecoflowRequest({ method, path, accessKey, secretKey, ...extra });
  if (String(res.code) !== "0") throw new Error(`EcoFlow ${res.code}: ${res.message}`);
  return res.data;
}

fs.mkdirSync("logs", { recursive: true });
const raw = fs.createWriteStream(`logs/probe-${new Date().toISOString().slice(0, 10)}.jsonl`, { flags: "a" });
const keep = (src, payload) => raw.write(JSON.stringify({ ts: Date.now(), src, ...payload }) + "\n");

const devices = (await api("GET", "/iot-open/sign/device/list")) ?? [];
for (const d of devices) console.log(stamp(), "device", d.productName ?? d.deviceName, `…${String(d.sn).slice(-4)}`, d.online ? "online" : "OFFLINE");
const sn = process.env.ECOFLOW_SN ?? devices[0]?.sn;
if (!sn) throw new Error("No device on this account");

const cert = await api("GET", "/iot-open/sign/certification");
const base = `/open/${cert.certificateAccount}/${sn}`;
const client = mqtt.connect(`${cert.protocol}://${cert.url}:${cert.port}`, {
  username: cert.certificateAccount,
  password: cert.certificatePassword,
});
client.on("connect", () => {
  console.log(stamp(), "mqtt  connected");
  client.subscribe([`${base}/quota`, `${base}/status`]);
});
client.on("error", (e) => console.log(stamp(), "mqtt  error", e.message));
client.on("message", (topic, buf) => {
  const text = buf.toString();
  const name = topic.slice(base.length);
  keep("mqtt", { topic: name, text });
  let msg = {};
  try {
    msg = JSON.parse(text);
  } catch {}
  const params = msg.params ?? msg.param ?? msg;
  console.log(stamp(), "mqtt ", name, msg.typeCode ?? "", `${Object.keys(params).length} keys`, brief(params));
});

let prev = {};
async function poll() {
  try {
    const all = (await api("GET", "/iot-open/sign/device/quota/all", { queryString: `?sn=${sn}`, signData: { sn } })) ?? {};
    keep("http", { data: all });
    const keys = Object.keys(all);
    const changed = keys.filter((k) => JSON.stringify(all[k]) !== JSON.stringify(prev[k]));
    console.log(stamp(), "http ", `${changed.length}/${keys.length} keys changed`, brief(all), changed.slice(0, 6).join(","));
    prev = all;

    // the per-quota endpoint may ask the device itself instead of the cloud copy
    const body = { sn, params: { quotas: ["pd.soc", "pd.wattsInSum", "pd.wattsOutSum"] } };
    const some = await api("POST", "/iot-open/sign/device/quota", { body });
    keep("post", { data: some });
    console.log(stamp(), "post ", brief(some));
  } catch (e) {
    console.log(stamp(), "http  error", e.message);
  }
}
poll();
setInterval(poll, POLL_MS);
