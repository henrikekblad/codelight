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
    // Companion pushes data; no polling URL needed on device side
    String companionSecret;  // optional shared secret for POST /status
};

extern Config cfg;

bool configLoad();
void configSave();
void configDefaults();
