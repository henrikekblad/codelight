#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266mDNS.h>
#include <ESPAsyncWebServer.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <Crypto.h>
#include <libb64/cdecode.h>
#include <time.h>
#include "config.h"
#include "display.h"
#include "webserver.h"

static AsyncWebServer   server(80);
static WebSocketsClient wsClient;

static String mdnsName;

static bool          wsConnected      = false;
static bool          wsBegun          = false;
static bool          wsAuthFailed     = false;
static bool          wsHelloSent      = false;
static unsigned long wsLastDiscoverMs = 0;
static IPAddress     wsLastEndpointIp;
static uint16_t      wsLastEndpointPort = 8765;
static bool          wsHaveLastEndpoint = false;
#define WS_DISCOVER_MS 15000UL
#define WS_SLEEP_DISCOVER_MS 60000UL
#define WS_MDNS_TIMEOUT_MS 2000
#define WS_SLEEP_MDNS_TIMEOUT_MS 1000
#define WS_RECONNECT_MS 15000UL
#define WS_SLEEP_CONNECT_WINDOW_MS 5000UL

static unsigned long lastClockMs = 0;
static bool          displayReady = false;
static unsigned long wsSleepProbeUntilMs = 0;

// Sleep screen triggers (both individually configurable on the config page)
static unsigned long lastCompanionMs = 0;   // last time a companion connection was up
static unsigned long lastActiveMs    = 0;   // last time status was working/waiting
#define SLEEP_AFTER_MS      (10UL * 60UL * 1000UL)   // disconnected
#define IDLE_SLEEP_AFTER_MS (60UL * 60UL * 1000UL)   // connected but idle

static const char* ntpServer = "pool.ntp.org";

// RTC memory slot 0: crash-guard for displayInit().
// If displayInit() crashes, next boot sees DISPLAY_TRYING and skips it.
#define DISPLAY_OK     0x12345678u
#define DISPLAY_TRYING 0xDEADBEEFu

// HMAC-SHA256(secret, nonce) as lowercase hex — proves knowledge of the
// secret to the daemon without ever transmitting it.
static String hmacHex(const String& secret, const String& nonce) {
    uint8_t out[32];
    experimental::crypto::SHA256::hmac(nonce.c_str(), nonce.length(),
                                       secret.c_str(), secret.length(), out, sizeof(out));
    String hex;
    hex.reserve(64);
    char b[3];
    for (size_t i = 0; i < sizeof(out); i++) {
        snprintf(b, sizeof(b), "%02x", out[i]);
        hex += b;
    }
    return hex;
}

// Identify as a screen so the companion sends the screen config variant
// (bitmap logos instead of SVGs). The config message is its reply.
static void sendHello() {
    if (wsHelloSent) return;
    wsClient.sendTXT("{\"type\":\"subscribe\",\"client\":\"screen\"}");
    wsHelloSent = true;
}

// #rrggbb → RGB565.
static uint16_t parseColor565(const char* hex) {
    if (!hex || hex[0] != '#' || strlen(hex) != 7) return 0xFFFF;
    long v = strtol(hex + 1, nullptr, 16);
    uint8_t r = (v >> 16) & 0xFF, g = (v >> 8) & 0xFF, b = v & 0xFF;
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

// Base64 → exactly LOGO_BYTES, else reject.
static bool decodeLogoBitmap(const char* b64, uint8_t* out) {
    if (!b64 || !*b64) return false;
    char decoded[LOGO_BYTES + 4];
    base64_decodestate state;
    base64_init_decodestate(&state);
    int n = base64_decode_block(b64, strlen(b64), decoded, &state);
    if (n != LOGO_BYTES) return false;
    memcpy(out, decoded, LOGO_BYTES);
    return true;
}

static void applyConfig(JsonDocument& doc) {
    if (doc["utc_offset"].is<long>()) {
        long off = doc["utc_offset"].as<long>();
        configTime(off, 0L, ntpServer);
        dbgLog("[ws] utc_offset=" + String(off) + "s");
    }
    JsonObjectConst agents = doc["agents"];
    if (agents.isNull()) return;
    displayClearAgentLogos();
    uint8_t bits[LOGO_BYTES];
    int added = 0;
    for (JsonPairConst kv : agents) {
        JsonObjectConst meta = kv.value();
        if (!decodeLogoBitmap(meta["logo_bitmap"] | "", bits)) continue;
        if (displayAddAgentLogo(parseColor565(meta["color"] | ""), bits)) added++;
    }
    dbgLog("[ws] config: " + String(added) + " agent logos");
}

static void applyStatus(uint8_t* payload, size_t length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) return;

    ClaudeStatus prevStatus = displayData.status;
    bool wasConnected = displayData.connected;
    bool wasSleeping = displayReady && displaySleeping();

    displayData.weeklyPct    = doc["weekly_pct"]   | 0.0f;
    displayData.sessionPct   = doc["session_pct"]  | 0.0f;
    displayData.weeklyReset  = doc["weekly_reset"].as<String>();
    displayData.sessionReset = doc["session_reset"].as<String>();
    displayData.agentId      = doc["agent_id"].as<String>();
    displayData.agentDisplay = doc["agent_display"].as<String>();
    // Empty titles mean the server has no limit info for this agent (e.g.
    // Copilot without billing data) — the bars are hidden, not synthesized.
    displayData.weeklyTitle  = doc["weekly_title"].as<String>();
    displayData.sessionTitle = doc["session_title"].as<String>();
    displayData.sessions     = doc["sessions"]     | 0;
    displayData.connected    = true;
    displayData.authFailed   = false;

    const char* st = doc["status"] | "idle";
    if      (strcmp(st, "working") == 0) displayData.status = STATUS_WORKING;
    else if (strcmp(st, "waiting") == 0) displayData.status = STATUS_WAITING;
    else                                  displayData.status = STATUS_INACTIVE;

    dbgLog(String("status=") + st +
           " session=" + String((int)(displayData.sessionPct * 100)) + "%" +
           " weekly="  + String((int)(displayData.weeklyPct  * 100)) + "%");

    if (displayData.status != STATUS_INACTIVE) lastActiveMs = millis();

    if (displayReady) {
        // Wake when a disconnected screen receives its first status after
        // reconnect ("person comes home"), even if the status is idle. For an
        // already-connected sleeping screen, only working/waiting status
        // changes wake it; routine usage polls stay quiet.
        if ((wasSleeping && !wasConnected)
            || (displayData.status != STATUS_INACTIVE && displayData.status != prevStatus))
            displayWake();
        displayUpdate();
    }
}

static void wsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
            wsHelloSent = false;
            if (!wsConnected && !wsAuthFailed) break;  // suppress library retry spam
            wsConnected = false;
            if (wsAuthFailed) {
                dbgLog(F("[ws] disconnected – auth failed, reconnect disabled"));
                wsBegun = true;
            } else {
                dbgLog(F("[ws] disconnected"));
                wsBegun = false;
                wsLastDiscoverMs = millis();  // reset timer; retry after WS_DISCOVER_MS
            }
            wsSleepProbeUntilMs = 0;
            displayData.connected = false;
            displayData.authFailed = wsAuthFailed;
            if (wsAuthFailed) {
                displayData.status = STATUS_AUTH_FAILED;
            }
            lastCompanionMs = millis();
            if (displayReady) displayUpdate();
            break;

        case WStype_CONNECTED:
            dbgLog(F("[ws] connected"));
            wsConnected = true;
            wsAuthFailed = false;
            wsHelloSent = false;
            wsSleepProbeUntilMs = 0;
            displayData.authFailed = false;
            lastActiveMs = millis();
            if (displayReady) displayWake();
            // Auth happens via the daemon's challenge (see WStype_TEXT) so the
            // secret never crosses the wire — nothing to send on connect.
            break;

        case WStype_TEXT:
            {
                JsonDocument doc;
                if (deserializeJson(doc, payload, length)) break;

                if (strcmp(doc["error"] | "", "unauthorized") == 0) {
                    dbgLog(F("[ws] unauthorized – check companion secret"));
                    wsAuthFailed = true;
                    wsConnected = false;
                    wsClient.disconnect();
                    displayData.weeklyPct = 0.0f;
                    displayData.sessionPct = 0.0f;
                    displayData.weeklyReset = "--";
                    displayData.sessionReset = "--";
                    displayData.weeklyTitle = "";
                    displayData.sessionTitle = "";
                    displayData.agentDisplay = "";
                    displayData.agentId = "";
                    displayData.sessions = 0;
                    displayData.connected = false;
                    displayData.authFailed = true;
                    displayData.status = STATUS_AUTH_FAILED;
                    if (displayReady) displayUpdate();
                    break;
                }

                if (strcmp(doc["type"] | "", "challenge") == 0) {
                    if (cfg.companionSecret.length() > 0) {
                        String nonce = doc["nonce"] | "";
                        String proof = hmacHex(cfg.companionSecret, nonce);
                        String reply = "{\"auth_hmac\":\"" + proof + "\"}";
                        wsClient.sendTXT(reply);
                        sendHello();
                    }
                    break;
                }

                // Any non-challenge frame means the auth phase is over; an
                // unsecured daemon sends no challenge, so say hello here.
                sendHello();

                if (strcmp(doc["type"] | "", "config") == 0) {
                    applyConfig(doc);
                    break;
                }

                // Any other typed frame (e.g. permission events for subscribed
                // clients) is not a status payload — never feed it to the display
                if (strlen(doc["type"] | "") > 0) break;

                applyStatus(payload, length);
            }
            break;

        default:
            break;
    }
}

static void connectWifi() {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    // Always-powered device: modem sleep only causes missed packets and
    // latency spikes for push updates, the web UI, and OTA uploads
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.disconnect();
    delay(100);
    Serial.flush();

    for (uint8_t i = 0; i < cfg.wifiCount; i++) {
        Serial.print(F("  Trying: "));
        Serial.println(cfg.wifi[i].ssid);
        Serial.flush();

        WiFi.begin(cfg.wifi[i].ssid.c_str(), cfg.wifi[i].password.c_str());

        for (int j = 0; j < 20; j++) {
            if (WiFi.status() == WL_CONNECTED) break;
            delay(500);
            Serial.print(F("."));
            Serial.flush();
        }
        Serial.println();

        if (WiFi.status() == WL_CONNECTED) {
            dbgLog("WiFi connected: " + WiFi.localIP().toString() +
                   " RSSI=" + String(WiFi.RSSI()) + "dBm");
            configTime(0, 0, ntpServer);  // offset corrected when companion sends config
            return;
        }

        WiFi.disconnect();
        delay(100);
    }

    // No network reachable → AP mode
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, nullptr, 6);  // ch 6: avoids ch 1/11 used by most home routers
    Serial.println(F("  AP: " AP_SSID " / 192.168.4.1"));
    Serial.flush();
}

static void showWifiStatus() {
    if (!displayReady) return;

    if (WiFi.status() == WL_CONNECTED) {
        // Show mDNS name + IP as a brief splash — clear only what we draw
        // so we don't wipe the chrome that displayInit() just established.
        tft.setTextFont(2);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setCursor(6, 6);
        tft.print(mdnsName + ".local");
        tft.fillRect(tft.getCursorX(), 6, 240 - tft.getCursorX(), 16, TFT_BLACK);
        tft.setCursor(6, 26);
        tft.print(WiFi.localIP().toString());
        tft.fillRect(tft.getCursorX(), 26, 240 - tft.getCursorX(), 16, TFT_BLACK);
        delay(2000);
    } else {
        tft.fillScreen(TFT_NAVY);
        tft.setTextFont(2);

        tft.setTextColor(0xFD20, TFT_NAVY);
        tft.setCursor(6, 6);
        tft.print(F("Setup mode"));

        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setCursor(6, 28);
        tft.print(F("1. Connect to WiFi:"));
        tft.setCursor(6, 46);
        tft.setTextColor(0x07FF, TFT_NAVY);
        tft.print(F(AP_SSID));

        tft.setTextColor(TFT_WHITE, TFT_NAVY);
        tft.setCursor(6, 68);
        tft.print(F("2. Open browser:"));
        tft.setCursor(6, 86);
        tft.setTextColor(0x07FF, TFT_NAVY);
        tft.print(F("192.168.4.1"));

        tft.setTextColor(0xAD75, TFT_NAVY);
        tft.setCursor(6, 110);
        tft.print(F("Add networks, save,"));
        tft.setCursor(6, 126);
        tft.print(F("then reboot."));
    }
}

static void sanitiseMdnsName(const String& name, String& out) {
    out = "";
    for (char c : name) {
        if (isalnum(c)) out += (char)tolower(c);
        else if (c == '-' || c == ' ') out += '-';
    }
    if (out.length() == 0) out = "codelight-screen";
}

static void beginWs(IPAddress ip, uint16_t port, const String& label) {
    dbgLog("[ws] connecting to " + label + " at " + ip.toString() + ":" + String(port));
    wsClient.begin(ip, port, "/");
    wsClient.onEvent(wsEvent);
    wsClient.setReconnectInterval(WS_RECONNECT_MS);
    wsClient.enableHeartbeat(15000, 3000, 2);
    wsBegun = true;
}

static void beginWsHost(const String& host) {
    dbgLog("[ws] direct connect to " + host);
    wsClient.begin(host, 8765, "/");
    wsClient.onEvent(wsEvent);
    wsClient.setReconnectInterval(WS_RECONNECT_MS);
    wsClient.enableHeartbeat(15000, 3000, 2);
    wsBegun = true;
}

static bool tryLastEndpoint() {
    if (!wsHaveLastEndpoint || wsAuthFailed) return false;
    beginWs(wsLastEndpointIp, wsLastEndpointPort, "last-known companion");
    return true;
}

static void tryDiscover(uint16_t mdnsTimeoutMs = WS_MDNS_TIMEOUT_MS) {
    if (wsAuthFailed) return;

    if (cfg.companionHost.length() > 0) {
        beginWsHost(cfg.companionHost);
        return;
    }

    dbgLog("[ws] querying mDNS _codelight._tcp (" + String(mdnsTimeoutMs) + "ms timeout)...");
    int n = MDNS.queryService("codelight", "tcp", mdnsTimeoutMs);
    dbgLog("[ws] mDNS query returned n=" + String(n));
    if (n <= 0) {
        dbgLog(F("[ws] not found, will retry"));
        return;
    }
    for (int i = 0; i < n; i++) {
        dbgLog("[ws] found[" + String(i) + "] " + MDNS.hostname(i) +
               " " + MDNS.IP(i).toString() + ":" + String(MDNS.port(i)));
    }

    // Pick by configured name, or fall back to first result
    int idx = 0;
    if (cfg.companionName.length() > 0) {
        bool found = false;
        for (int i = 0; i < n; i++) {
            String h = MDNS.hostname(i);
            if (h.indexOf('.') > 0) h = h.substring(0, h.indexOf('.'));
            if (h == cfg.companionName) {
                idx = i;
                found = true;
                break;
            }
        }
        if (!found) {
            dbgLog("[ws] companion '" + cfg.companionName + "' not found in results, will retry");
            return;
        }
    }

    IPAddress ip   = MDNS.IP(idx);
    uint16_t  port = MDNS.port(idx);
    wsLastEndpointIp = ip;
    wsLastEndpointPort = port;
    wsHaveLastEndpoint = true;
    beginWs(ip, port, MDNS.hostname(idx));
}

void setup() {
    // Blank the display immediately — TFT_BL floats high-z after reset and
    // the PCB pull defaults to ON, so the old display content is visible for
    // the entire WiFi-init phase (~10 s) unless we kill it here first.
    // displayInit() will re-enable the backlight once the screen is clean.
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, HIGH);  // active LOW — HIGH = off

    Serial.begin(115200);
    delay(200);
    Serial.println(F("\n\n=== codelight boot ==="));
    Serial.flush();

    // --- 1. Config ---
    Serial.println(F("[1] Loading config..."));
    Serial.flush();
    configLoad();
    Serial.print(F("    wifiCount=")); Serial.println(cfg.wifiCount);
    Serial.flush();

    // --- 2. WiFi / AP  (before display so OTA works even if display crashes) ---
    Serial.println(F("[2] WiFi..."));
    Serial.flush();
    connectWifi();

    // --- 3. mDNS + webserver ---
    Serial.println(F("[3] mDNS + webserver..."));
    Serial.flush();
    sanitiseMdnsName(cfg.deviceName, mdnsName);
    if (MDNS.begin(mdnsName.c_str())) {
        MDNS.addService("http", "tcp", 80);
        Serial.println("    mDNS: " + mdnsName + ".local");
    }
    webserverInit(server);
    Serial.println(F("    webserver OK"));
    Serial.flush();

    // --- 4. Display (RTC crash-guard skips if previous boot crashed here) ---
    uint32_t rtcFlag = 0;
    ESP.rtcUserMemoryRead(0, &rtcFlag, sizeof(rtcFlag));
    Serial.print(F("[4] Display. rtcFlag=0x"));
    Serial.println(rtcFlag, HEX);
    Serial.flush();

    if (rtcFlag == DISPLAY_TRYING) {
        Serial.println(F("    Skipping display (crashed last boot)."));
        Serial.println(F("    OTA: http://192.168.4.1/update"));
        Serial.flush();
    } else {
        rtcFlag = DISPLAY_TRYING;
        ESP.rtcUserMemoryWrite(0, &rtcFlag, sizeof(rtcFlag));

        displayInit();

        rtcFlag = DISPLAY_OK;
        ESP.rtcUserMemoryWrite(0, &rtcFlag, sizeof(rtcFlag));
        displayReady = true;
        Serial.println(F("    Display OK"));
        Serial.flush();
    }

    showWifiStatus();

    if (displayReady && WiFi.status() == WL_CONNECTED) {
        displayData.connected = false;
        displayData.authFailed = false;
        displayUpdate();
    }

    // --- 5. Initial WS discovery ---
    if (WiFi.status() == WL_CONNECTED) {
        tryDiscover();
        wsLastDiscoverMs = millis();
    }

    lastClockMs = millis();
    lastCompanionMs = millis();
    lastActiveMs = millis();
    Serial.println(F("=== setup complete ==="));
    Serial.flush();
}

void loop() {
    MDNS.update();
    webserverLoop();   // synchronous :81 updater (blocks here during an update,
                       // then ESP8266HTTPUpdateServer reboots the device itself)

    unsigned long now = millis();

    if (displayReady && now - lastClockMs >= 1000) {
        lastClockMs = now;
        displayUpdateClock();
    }

    if (displayReady) {
        if (!displaySleeping()) {
            bool discSleep = cfg.sleepOnDisconnect
                && !displayData.connected && !displayData.authFailed
                && now - lastCompanionMs >= SLEEP_AFTER_MS;
            bool idleSleep = cfg.sleepOnIdle
                && displayData.connected && displayData.status == STATUS_INACTIVE
                && now - lastActiveMs >= IDLE_SLEEP_AFTER_MS;
            if (discSleep || idleSleep) displaySleepStart();
        }
        displaySleepTick(now);
    }

    if (WiFi.status() == WL_CONNECTED) {
        // Re-discover companion via mDNS when not yet connected
        bool sleepProbe = false;
        unsigned long discoverInterval = displaySleeping() ? WS_SLEEP_DISCOVER_MS : WS_DISCOVER_MS;
        if (!wsAuthFailed && !wsBegun && (now - wsLastDiscoverMs >= discoverInterval)) {
            wsLastDiscoverMs = now;
            if (displaySleeping() && tryLastEndpoint()) {
                // mDNS can miss a freshly restarted companion. During sleep,
                // first try the last endpoint that was known to work; if it no
                // longer answers, the bounded probe window below resets state
                // and the next interval can discover again.
            } else {
                tryDiscover(displaySleeping() ? WS_SLEEP_MDNS_TIMEOUT_MS : WS_MDNS_TIMEOUT_MS);
            }
            sleepProbe = displaySleeping();
            if (sleepProbe && wsBegun)
                wsSleepProbeUntilMs = now + WS_SLEEP_CONNECT_WINDOW_MS;
        }
        // WebSocketsClient reconnect attempts are synchronous on ESP8266. When
        // the companion is offline a failed TCP connect can block for several
        // seconds; if this runs during the sleep animation the screen appears
        // frozen. While asleep and disconnected, let the lightweight discovery
        // timer above decide when to try again. After a sleep discovery/direct-
        // host probe, allow a short handshake window so a found companion can
        // finish TCP + WebSocket setup and deliver the status that wakes us.
        bool sleepConnectWindow = displaySleeping()
            && wsBegun && !wsConnected
            && wsSleepProbeUntilMs != 0
            && now < wsSleepProbeUntilMs;
        if (!displaySleeping() || wsConnected || sleepProbe || sleepConnectWindow)
            wsClient.loop();
        else if (displaySleeping() && wsBegun && !wsConnected && wsSleepProbeUntilMs != 0) {
            wsBegun = false;
            wsSleepProbeUntilMs = 0;
            dbgLog(F("[ws] sleep probe timed out, will retry"));
        }
    }

    yield();
}
