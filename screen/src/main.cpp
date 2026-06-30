#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266mDNS.h>
#include <ESPAsyncWebServer.h>
#include <ElegantOTA.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
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
static unsigned long wsLastDiscoverMs = 0;
#define WS_DISCOVER_MS 15000UL

static unsigned long lastClockMs = 0;
static bool          displayReady = false;

static const char* ntpServer = "pool.ntp.org";

// RTC memory slot 0: crash-guard for displayInit().
// If displayInit() crashes, next boot sees DISPLAY_TRYING and skips it.
#define DISPLAY_OK     0x12345678u
#define DISPLAY_TRYING 0xDEADBEEFu

static void applyStatus(uint8_t* payload, size_t length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) return;

    displayData.weeklyPct    = doc["weekly_pct"]   | 0.0f;
    displayData.sessionPct   = doc["session_pct"]  | 0.0f;
    displayData.weeklyReset  = doc["weekly_reset"].as<String>();
    displayData.sessionReset = doc["session_reset"].as<String>();
    displayData.sessions     = doc["sessions"]     | 0;
    displayData.connected    = true;
    displayData.authFailed   = false;

    const char* st = doc["status"] | "inactive";
    if      (strcmp(st, "working") == 0) displayData.status = STATUS_WORKING;
    else if (strcmp(st, "waiting") == 0) displayData.status = STATUS_WAITING;
    else                                  displayData.status = STATUS_INACTIVE;

    dbgLog(String("status=") + st +
           " session=" + String((int)(displayData.sessionPct * 100)) + "%" +
           " weekly="  + String((int)(displayData.weeklyPct  * 100)) + "%");

    if (displayReady) displayUpdate();
}

static void wsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
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
            displayData.connected = false;
            displayData.authFailed = wsAuthFailed;
            if (wsAuthFailed) {
                displayData.status = STATUS_AUTH_FAILED;
            }
            if (displayReady) displayUpdate();
            break;

        case WStype_CONNECTED:
            dbgLog(F("[ws] connected"));
            wsConnected = true;
            wsAuthFailed = false;
            displayData.authFailed = false;
            if (cfg.companionSecret.length() > 0) {
                String auth = "{\"auth\":\"" + cfg.companionSecret + "\"}";
                wsClient.sendTXT(auth);
            }
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
                    displayData.connected = false;
                    displayData.authFailed = true;
                    displayData.status = STATUS_AUTH_FAILED;
                    if (displayReady) displayUpdate();
                    break;
                }

                if (strcmp(doc["type"] | "", "config") == 0) {
                    if (doc["utc_offset"].is<long>()) {
                        long off = doc["utc_offset"].as<long>();
                        configTime(off, 0L, ntpServer);
                        dbgLog("[ws] utc_offset=" + String(off) + "s");
                    }
                    break;
                }

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
            dbgLog("WiFi connected: " + WiFi.localIP().toString());
            configTime(0, 0, ntpServer);  // offset corrected when companion sends config
            return;
        }

        WiFi.disconnect();
        delay(100);
    }

    // No network reachable → AP mode
    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID);
    Serial.println(F("  AP: " AP_SSID " / 192.168.4.1"));
    Serial.flush();
}

static void showWifiStatus() {
    if (!displayReady) return;

    if (WiFi.status() == WL_CONNECTED) {
        tft.fillScreen(TFT_BLACK);
        tft.setTextFont(2);
        tft.setTextColor(TFT_WHITE, TFT_BLACK);
        tft.setCursor(6, 6);
        tft.print(mdnsName + ".local");
        tft.setCursor(6, 26);
        tft.print(WiFi.localIP().toString());
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
    if (out.length() == 0) out = "claude-screen";
}

static void tryDiscover() {
    if (wsAuthFailed) return;

    if (cfg.companionHost.length() > 0) {
        dbgLog("[ws] direct connect to " + cfg.companionHost);
        wsClient.begin(cfg.companionHost, 8765, "/");
        wsClient.onEvent(wsEvent);
        wsClient.enableHeartbeat(15000, 3000, 2);
        wsBegun = true;
        return;
    }

    dbgLog(F("[ws] querying mDNS _codelight._tcp (2s timeout)..."));
    int n = MDNS.queryService("codelight", "tcp", 2000);
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
    dbgLog("[ws] connecting to " + MDNS.hostname(idx) + " at " + ip.toString() + ":" + String(port));
    wsClient.begin(ip, port, "/");
    wsClient.onEvent(wsEvent);
    wsClient.enableHeartbeat(15000, 3000, 2);
    wsBegun = true;
}

void setup() {
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
    Serial.println(F("=== setup complete ==="));
    Serial.flush();
}

void loop() {
    MDNS.update();
    ElegantOTA.loop();

    unsigned long now = millis();

    if (displayReady && now - lastClockMs >= 1000) {
        lastClockMs = now;
        displayUpdateClock();
    }

    if (WiFi.status() == WL_CONNECTED) {
        // Re-discover companion via mDNS when not yet connected
        if (!wsAuthFailed && !wsBegun && (now - wsLastDiscoverMs >= WS_DISCOVER_MS)) {
            wsLastDiscoverMs = now;
            tryDiscover();
        }
        wsClient.loop();
    }

    yield();
}
