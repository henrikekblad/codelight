#include "config.h"
#include <LittleFS.h>

Config cfg;

void configDefaults() {
    cfg.wifiCount = 0;
    cfg.deviceName = "claude-screen";
    cfg.companionName   = "";
    cfg.companionHost   = "";
    cfg.companionSecret = "";
    cfg.sleepOnDisconnect = true;
    cfg.sleepOnIdle       = true;
}

bool configLoad() {
    if (!LittleFS.begin()) {
        Serial.println(F("LittleFS mount failed – formatting..."));
        LittleFS.format();
        if (!LittleFS.begin()) {
            Serial.println(F("LittleFS format failed"));
            configDefaults();
            return false;
        }
        Serial.println(F("LittleFS formatted OK"));
    }
    if (!LittleFS.exists(CONFIG_FILE)) {
        configDefaults();
        return false;
    }
    File f = LittleFS.open(CONFIG_FILE, "r");
    if (!f) { configDefaults(); return false; }

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, f);
    f.close();
    if (err) { configDefaults(); return false; }

    cfg.deviceName      = doc["deviceName"]      | "claude-screen";
    cfg.companionName   = doc["companionName"]   | "";
    cfg.companionHost   = doc["companionHost"]   | "";
    cfg.companionSecret = doc["companionSecret"] | "";
    cfg.sleepOnDisconnect = doc["sleepOnDisconnect"] | true;
    cfg.sleepOnIdle       = doc["sleepOnIdle"]       | true;

    JsonArray nets = doc["wifi"].as<JsonArray>();
    cfg.wifiCount = 0;
    for (JsonObject net : nets) {
        if (cfg.wifiCount >= MAX_WIFI_NETWORKS) break;
        cfg.wifi[cfg.wifiCount].ssid     = net["ssid"].as<String>();
        cfg.wifi[cfg.wifiCount].password = net["password"].as<String>();
        cfg.wifiCount++;
    }
    return true;
}

void configSave() {
    File f = LittleFS.open(CONFIG_FILE, "w");
    if (!f) return;

    JsonDocument doc;
    doc["deviceName"]      = cfg.deviceName;
    doc["companionName"]   = cfg.companionName;
    doc["companionHost"]   = cfg.companionHost;
    doc["companionSecret"] = cfg.companionSecret;
    doc["sleepOnDisconnect"] = cfg.sleepOnDisconnect;
    doc["sleepOnIdle"]       = cfg.sleepOnIdle;

    JsonArray nets = doc["wifi"].to<JsonArray>();
    for (uint8_t i = 0; i < cfg.wifiCount; i++) {
        JsonObject net = nets.add<JsonObject>();
        net["ssid"]     = cfg.wifi[i].ssid;
        net["password"] = cfg.wifi[i].password;
    }
    serializeJson(doc, f);
    f.close();
}
