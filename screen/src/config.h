#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#define MAX_WIFI_NETWORKS 3
#define CONFIG_FILE "/config.json"
#define AP_SSID "claude-screen-setup"

struct WifiEntry {
    String ssid;
    String password;
};

struct Config {
    WifiEntry wifi[MAX_WIFI_NETWORKS];
    uint8_t wifiCount;
    String deviceName;
    String companionName;    // mDNS instance name to connect to (e.g. "henrik-laptop"); blank = first found
    String companionSecret;  // optional shared secret for WebSocket auth
};

extern Config cfg;

bool configLoad();
void configSave();
void configDefaults();
