#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#define MAX_WIFI_NETWORKS 3
#define CONFIG_FILE "/config.json"
#define AP_SSID "codelight-screen-setup"

struct WifiEntry {
    String ssid;
    String password;
};

struct Config {
    WifiEntry wifi[MAX_WIFI_NETWORKS];
    uint8_t wifiCount;
    String deviceName;
    String companionName;    // mDNS instance name to connect to (e.g. "my-laptop"); blank = first found
    String companionHost;    // direct IP or hostname, bypasses mDNS when set
    String companionSecret;  // optional shared secret for WebSocket auth
    bool sleepOnDisconnect;  // sleep screen after 10 min without companion
    bool sleepOnIdle;        // sleep screen after 1 h of idle status
};

extern Config cfg;

bool configLoad();
void configSave();
void configDefaults();
