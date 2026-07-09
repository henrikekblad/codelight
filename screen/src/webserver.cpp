#include "webserver.h"
#include "config.h"
#include "display.h"
#include <ArduinoJson.h>
#include <time.h>
#include "syncota.h"

// ── Debug log buffer ──────────────────────────────────────────────────────────
#define DBG_LINES    20
#define DBG_LINE_LEN 96

static char     _dbgBuf[DBG_LINES][DBG_LINE_LEN];
static uint16_t _dbgSeq = 0;

void dbgLog(const String& msg) {
    char line[DBG_LINE_LEN];
    time_t now = time(nullptr);
    if (now > 1000000000UL) {
        struct tm* t = localtime(&now);
        snprintf(line, sizeof(line), "[%02d:%02d:%02d] %s", t->tm_hour, t->tm_min, t->tm_sec, msg.c_str());
    } else {
        unsigned long ms = millis();
        snprintf(line, sizeof(line), "[+%lus] %s", ms / 1000, msg.c_str());
    }
    strncpy(_dbgBuf[_dbgSeq % DBG_LINES], line, DBG_LINE_LEN - 1);
    _dbgBuf[_dbgSeq % DBG_LINES][DBG_LINE_LEN - 1] = '\0';
    _dbgSeq++;
    Serial.println(line);
}

static const char INDEX_HTML[] PROGMEM = R"rawhtml(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>codelight Screen Config</title>
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
  .chk{display:flex;align-items:center;gap:.5rem;color:#c9d1d9;font-size:.85rem;margin-bottom:.4rem;cursor:pointer}
  #fw{display:flex;gap:.5rem;align-items:center;margin-bottom:.4rem}
  #fw input[type=file]{flex:1;color:#8b949e;font-size:.8rem}
  #fw button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:.45rem .8rem;cursor:pointer;font-size:.85rem;white-space:nowrap}
  #fw button:hover{background:#30363d}
  .hint{color:#8b949e;font-size:.75rem;margin-bottom:.6rem}
  #msg{margin-top:.8rem;font-size:.85rem;color:#3fb950;min-height:1.2rem}
  a.ota{display:inline-block;margin-top:1.2rem;color:#58a6ff;font-size:.85rem;text-decoration:none}
  a.ota:hover{text-decoration:underline}
</style>
</head>
<body>
<h1>codelight Screen</h1>

<form id="cfg">
  <h2>Device</h2>
  <label>Device name (used as mDNS hostname)</label>
  <input type="text" id="deviceName" placeholder="codelight-screen" maxlength="32">

  <label>Companion name <span style="color:#8b949e;font-size:.8rem">(--name passed to codelight.py; blank = first found)</span></label>
  <input type="text" id="companionName" placeholder="e.g. my-laptop" maxlength="64">

  <label>Companion host <span style="color:#8b949e;font-size:.8rem">(IP or hostname – bypasses mDNS when set)</span></label>
  <input type="text" id="companionHost" placeholder="e.g. 192.168.1.100" maxlength="64">

  <label>Companion secret <span style="color:#8b949e;font-size:.8rem">(optional – match --secret in codelight.py)</span></label>
  <input type="password" id="companionSecret" placeholder="(set – leave blank to keep)">
  <label class="chk"><input type="checkbox" id="clearSecret"> Clear secret (remove authentication)</label>

  <h2>Display</h2>
  <label class="chk"><input type="checkbox" id="sleepOnDisconnect"> Sleep after 10 minutes when disconnected</label>
  <label class="chk"><input type="checkbox" id="sleepOnIdle"> Sleep after 1 hour of idle</label>

  <h2>WiFi networks <span style="color:#8b949e;font-size:.8rem">(up to 3, tried in order)</span></h2>
  <div id="wifi-list"></div>
  <button type="button" id="add-net">+ Add network</button>

  <br>
  <button type="submit">Save &amp; apply</button>
  <div id="msg"></div>
</form>

<h2>Firmware update</h2>
<form id="fw" method="POST" enctype="multipart/form-data">
  <input type="file" name="firmware" accept=".bin,.bin.gz" required>
  <button type="submit">Upload &amp; flash</button>
</form>
<div class="hint">The device flashes and reboots after upload &mdash; takes about 20 seconds.</div>

<a class="ota" href="/debug">Debug log &#x2192;</a>

<script>
// The updater lives on the synchronous :81 server; a plain form POST across
// ports needs no CORS.
document.getElementById('fw').action = 'http://' + location.hostname + ':81/update';

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
  document.getElementById('companionHost').value = cfg.companionHost || '';
  document.getElementById('sleepOnDisconnect').checked = cfg.sleepOnDisconnect !== false;
  document.getElementById('sleepOnIdle').checked       = cfg.sleepOnIdle !== false;
  document.getElementById('companionSecret').placeholder =
    cfg.hasSecret ? '(set — leave blank to keep)' : 'leave blank to disable auth';
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

  const secretVal   = document.getElementById('companionSecret').value;
  const clearSecret = document.getElementById('clearSecret').checked;
  const body = {
    deviceName:      document.getElementById('deviceName').value.trim(),
    companionName:   document.getElementById('companionName').value.trim(),
    companionHost:   document.getElementById('companionHost').value.trim(),
    // Empty field = keep existing secret; clearSecret checkbox = explicitly remove it
    companionSecret: clearSecret ? '' : secretVal,
    clearSecret,
    sleepOnDisconnect: document.getElementById('sleepOnDisconnect').checked,
    sleepOnIdle:       document.getElementById('sleepOnIdle').checked,
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

static const char DEBUG_HTML[] PROGMEM = R"rawhtml(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>codelight debug</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:monospace;background:#0d1117;color:#3fb950;padding:12px;height:100vh;display:flex;flex-direction:column}
h1{font-size:.85rem;color:#58a6ff;margin-bottom:8px;flex-shrink:0}
#slp{margin-left:12px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:4px;padding:2px 8px;cursor:pointer;font-family:monospace;font-size:.78rem}
#slp:hover{background:#30363d}
#log{flex:1;overflow-y:auto;font-size:.78rem;line-height:1.5;white-space:pre-wrap;word-break:break-all}
.err{color:#f85149}
#screen{position:fixed;top:8px;right:8px;width:160px;height:160px;border:1px solid #30363d;border-radius:4px;background:#000}
</style>
</head>
<body>
<h1>codelight debug &mdash; <span id="st">connecting&hellip;</span><button id="slp">sleep</button></h1>
<img id="screen" src="/screendump" alt="screen">
<div id="log"></div>
<script>
let seq=0,el=document.getElementById('log'),st=document.getElementById('st'),sc=document.getElementById('screen');
let slp=document.getElementById('slp');
slp.onclick=()=>fetch('/api/debug/sleep',{method:'POST'})
  .then(r=>r.text()).then(t=>{slp.textContent=t==='sleeping'?'wake':'sleep';});
function poll(){
  fetch('/api/debug/log?from='+seq)
    .then(r=>r.json())
    .then(d=>{
      st.textContent='live';
      if(d.lines&&d.lines.length){
        let atBottom=el.scrollHeight-el.scrollTop<=el.clientHeight+4;
        d.lines.forEach(l=>{
          let div=document.createElement('div');
          div.textContent=l;
          el.appendChild(div);
        });
        seq=d.seq;
        if(atBottom)el.scrollTop=el.scrollHeight;
      }
      sc.src='/screendump?t='+Date.now();
    })
    .catch(()=>{st.textContent='disconnected';st.className='err';})
    .finally(()=>setTimeout(poll,1000));
}
poll();
</script>
</body>
</html>)rawhtml";

// GET /api/debug/log?from=N  – return log lines with index >= N
static void handleDebugLog(AsyncWebServerRequest* req) {
    uint16_t from = req->hasParam("from") ? (uint16_t)req->getParam("from")->value().toInt() : 0;
    if (_dbgSeq > DBG_LINES && from < _dbgSeq - DBG_LINES)
        from = _dbgSeq - DBG_LINES;
    JsonDocument doc;
    doc["seq"] = _dbgSeq;
    JsonArray lines = doc["lines"].to<JsonArray>();
    for (uint16_t i = from; i < _dbgSeq; i++)
        lines.add(_dbgBuf[i % DBG_LINES]);
    String out;
    serializeJson(doc, out);
    req->send(200, "application/json", out);
}

// GET /api/config  – return current config as JSON (passwords redacted)
static void handleGetConfig(AsyncWebServerRequest* req) {
    JsonDocument doc;
    doc["deviceName"]      = cfg.deviceName;
    doc["companionName"]   = cfg.companionName;
    doc["companionHost"]   = cfg.companionHost;
    doc["wifiCount"]       = cfg.wifiCount;
    doc["hasSecret"]       = cfg.companionSecret.length() > 0;
    doc["sleepOnDisconnect"] = cfg.sleepOnDisconnect;
    doc["sleepOnIdle"]       = cfg.sleepOnIdle;
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
    // Accumulate chunks into a heap buffer; _tempObject is auto-freed by request destructor
    if (index == 0) {
        req->_tempObject = malloc(total + 1);
        if (!req->_tempObject) { req->send(500); return; }
    }
    if (req->_tempObject)
        memcpy((uint8_t*)req->_tempObject + index, data, len);
    if (index + len < total) return;

    uint8_t* body = (uint8_t*)req->_tempObject;
    if (!body) { req->send(500); return; }
    body[total] = '\0';

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, body, total);
    free(body);
    req->_tempObject = nullptr;
    if (err) {
        dbgLog("POST /api/config parse error: " + String(err.c_str()));
        req->send(400);
        return;
    }

    if (doc["deviceName"].is<String>())
        cfg.deviceName = doc["deviceName"].as<String>();
    if (doc["companionName"].is<String>())
        cfg.companionName = doc["companionName"].as<String>();
    if (doc["companionHost"].is<String>())
        cfg.companionHost = doc["companionHost"].as<String>();
    if (doc["clearSecret"].is<bool>() && doc["clearSecret"].as<bool>()) {
        cfg.companionSecret = "";  // user explicitly cleared it
    } else if (doc["companionSecret"].is<String>()) {
        String s = doc["companionSecret"].as<String>();
        if (s.length() > 0)        // blank = keep existing
            cfg.companionSecret = s;
    }
    if (doc["sleepOnDisconnect"].is<bool>())
        cfg.sleepOnDisconnect = doc["sleepOnDisconnect"].as<bool>();
    if (doc["sleepOnIdle"].is<bool>())
        cfg.sleepOnIdle = doc["sleepOnIdle"].as<bool>();

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

    server.on("/debug", HTTP_GET, [](AsyncWebServerRequest* req) {
            req->send_P(200, "text/html", DEBUG_HTML);
    });

    server.on("/api/debug/log", HTTP_GET, handleDebugLog);

    // Reboot endpoint — used by buildAndOTAUpdate.sh after pushing WiFi config
    server.on("/api/reboot", HTTP_POST, [](AsyncWebServerRequest* req) {
        req->send(200, "text/plain", "rebooting");
        ESP.restart();
    });

    // Remote reboot (dev aid — e.g. clearing a wedged OTA session)
    server.on("/api/debug/reboot", HTTP_POST, [](AsyncWebServerRequest* req) {
        dbgLog(F("[debug] reboot requested"));
        req->send(200, "text/plain", "rebooting");
        ESP.restart();
    });

    // Toggle the sleep screen (testing aid on the debug page)
    server.on("/api/debug/sleep", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (displaySleeping()) {
            displayWake();
            dbgLog(F("[debug] wake triggered"));
            req->send(200, "text/plain", "awake");
        } else {
            displaySleepStart();
            dbgLog(F("[debug] sleep triggered"));
            req->send(200, "text/plain", displaySleeping() ? "sleeping" : "no display");
        }
    });

    server.on("/screendump", HTTP_GET, [](AsyncWebServerRequest* req) {
            req->send(200, "image/svg+xml", generateScreenSvg());
    });

    // Firmware updates live on the synchronous :81 server (see syncota.h);
    // keep the old URL working for browsers and bookmarks
    server.on("/update", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->redirect("http://" + req->host() + ":81/update");
    });

    server.begin();

    syncOtaInit();
}

void webserverLoop() {
    syncOtaLoop();
}
