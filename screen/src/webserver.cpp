#include "webserver.h"
#include "config.h"
#include <ArduinoJson.h>
#include <ElegantOTA.h>

static const char INDEX_HTML[] PROGMEM = R"rawhtml(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Screen Config</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px;max-width:480px;margin:0 auto}
  h1{font-size:1.2rem;margin-bottom:1.2rem;color:#58a6ff}
  h2{font-size:.9rem;color:#8b949e;margin:1.2rem 0 .6rem;text-transform:uppercase;letter-spacing:.05em}
  label{display:block;font-size:.85rem;margin-bottom:.2rem;color:#8b949e}
  input[type=text],input[type=password]{width:100%;padding:.45rem .6rem;background:#161b22;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;font-size:.9rem;margin-bottom:.7rem}
  input:focus{outline:none;border-color:#58a6ff}
  .net-row{display:flex;gap:.4rem;margin-bottom:.5rem}
  .net-row input{margin:0;flex:1}
  .net-row button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:0 .6rem;cursor:pointer;font-size:1rem}
  .net-row button:hover{background:#30363d}
  #add-net{background:none;border:1px dashed #30363d;color:#58a6ff;padding:.4rem .8rem;border-radius:6px;cursor:pointer;font-size:.85rem;width:100%;margin-top:.2rem}
  #add-net:hover{background:#161b22}
  button[type=submit]{background:#238636;border:none;color:#fff;padding:.55rem 1.2rem;border-radius:6px;cursor:pointer;font-size:.9rem;margin-top:.8rem}
  button[type=submit]:hover{background:#2ea043}
  #msg{margin-top:.8rem;font-size:.85rem;color:#3fb950;min-height:1.2rem}
  a.ota{display:inline-block;margin-top:1.2rem;color:#58a6ff;font-size:.85rem;text-decoration:none}
  a.ota:hover{text-decoration:underline}
</style>
</head>
<body>
<h1>Claude Screen</h1>

<form id="cfg">
  <h2>Device</h2>
  <label>Device name (used as mDNS hostname)</label>
  <input type="text" id="deviceName" placeholder="claude-screen" maxlength="32">

  <label>Companion name <span style="color:#8b949e;font-size:.8rem">(--name passed to codelight.py; blank = first found)</span></label>
  <input type="text" id="companionName" placeholder="e.g. henrik-laptop" maxlength="64">

  <label>Companion secret <span style="color:#8b949e;font-size:.8rem">(optional – match --secret in codelight.py)</span></label>
  <input type="password" id="companionSecret" placeholder="leave blank to disable auth">

  <h2>WiFi networks <span style="color:#8b949e;font-size:.8rem">(up to 3, tried in order)</span></h2>
  <div id="wifi-list"></div>
  <button type="button" id="add-net">+ Add network</button>

  <br>
  <button type="submit">Save &amp; apply</button>
  <div id="msg"></div>
</form>

<a class="ota" href="/update">Firmware update (OTA) &#x2192;</a>

<script>
const list = document.getElementById('wifi-list');

function addNetRow(ssid='', password='') {
  const row = document.createElement('div');
  row.className = 'net-row';
  row.innerHTML =
    '<input type="text" placeholder="SSID" value="' + escHtml(ssid) + '">' +
    '<input type="password" placeholder="' + (password ? '(saved – leave blank to keep)' : 'Password') + '" value="">' +
    '<button type="button" title="Remove">✕</button>';
  row.querySelector('button').onclick = () => row.remove();
  list.appendChild(row);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}

fetch('/api/config').then(r=>r.json()).then(cfg => {
  document.getElementById('deviceName').value    = cfg.deviceName    || '';
  document.getElementById('companionName').value = cfg.companionName || '';
  const nets = cfg.wifi || [];
  nets.forEach(n => addNetRow(n.ssid, ''));
  if (nets.length === 0) addNetRow();
}).catch(() => addNetRow());

document.getElementById('add-net').onclick = () => {
  if (list.children.length < 3) addNetRow();
};

document.getElementById('cfg').onsubmit = async (e) => {
  e.preventDefault();
  const msg = document.getElementById('msg');
  const rows = [...list.querySelectorAll('.net-row')];
  const wifi = rows.map(r => {
    const [s, p] = r.querySelectorAll('input');
    return {ssid: s.value.trim(), password: p.value};
  }).filter(n => n.ssid.length > 0);

  const body = {
    deviceName:      document.getElementById('deviceName').value.trim(),
    companionName:   document.getElementById('companionName').value.trim(),
    companionSecret: document.getElementById('companionSecret').value,
    wifi,
  };

  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    msg.style.color = res.ok ? '#3fb950' : '#f85149';
    msg.textContent = res.ok ? await res.text() : 'Error saving config.';
  } catch {
    msg.style.color = '#f85149';
    msg.textContent = 'Connection error.';
  }
};
</script>
</body>
</html>)rawhtml";

// GET /api/config  – return current config as JSON (passwords redacted)
static void handleGetConfig(AsyncWebServerRequest* req) {
    JsonDocument doc;
    doc["deviceName"]      = cfg.deviceName;
    doc["companionName"]   = cfg.companionName;
    doc["wifiCount"]       = cfg.wifiCount;
    doc["hasSecret"]       = cfg.companionSecret.length() > 0;
    JsonArray nets = doc["wifi"].to<JsonArray>();
    for (int i = 0; i < cfg.wifiCount; i++) {
        JsonObject n = nets.add<JsonObject>();
        n["ssid"] = cfg.wifi[i].ssid;
        n["password"] = ""; // never send password back
    }
    String out;
    serializeJson(doc, out);
    req->send(200, "application/json", out);
}

// POST /api/config  – update config
static void handlePostConfig(AsyncWebServerRequest* req, uint8_t* data, size_t len,
                             size_t index, size_t total) {
    if (index + len < total) return;

    JsonDocument doc;
    if (deserializeJson(doc, data, len)) { req->send(400); return; }

    if (doc["deviceName"].is<String>())
        cfg.deviceName = doc["deviceName"].as<String>();
    if (doc["companionName"].is<String>())
        cfg.companionName = doc["companionName"].as<String>();
    if (doc["companionSecret"].is<String>())
        cfg.companionSecret = doc["companionSecret"].as<String>();

    // Replace wifi list if provided
    if (doc["wifi"].is<JsonArray>()) {
        // Snapshot old list so we can preserve passwords when field left blank
        Config oldCfg = cfg;
        cfg.wifiCount = 0;
        for (JsonObject net : doc["wifi"].as<JsonArray>()) {
            if (cfg.wifiCount >= MAX_WIFI_NETWORKS) break;
            String ssid = net["ssid"] | "";
            String pass = net["password"] | "";
            if (ssid.length() == 0) continue;
            // Blank password in form → keep existing password for this SSID
            if (pass.length() == 0) {
                for (uint8_t k = 0; k < oldCfg.wifiCount; k++) {
                    if (oldCfg.wifi[k].ssid == ssid) { pass = oldCfg.wifi[k].password; break; }
                }
            }
            cfg.wifi[cfg.wifiCount].ssid     = ssid;
            cfg.wifi[cfg.wifiCount].password = pass;
            cfg.wifiCount++;
        }
    }
    configSave();
    req->send(200, "text/plain", "Saved. Reboot to apply WiFi changes.");
}

void webserverInit(AsyncWebServer& server) {
    server.on("/", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->send_P(200, "text/html", INDEX_HTML);
    });

    server.on("/api/config", HTTP_GET, handleGetConfig);

    server.on("/api/config", HTTP_POST,
        [](AsyncWebServerRequest* r){},
        nullptr,
        handlePostConfig);

    // ElegantOTA mounts /update (GET = page, POST = upload)
    ElegantOTA.begin(&server);

    server.begin();
}
