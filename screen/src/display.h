#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

enum ClaudeStatus {
    STATUS_INACTIVE = 0,
    STATUS_WORKING  = 1,
    STATUS_WAITING  = 2,
    STATUS_OFFLINE  = 3,
    STATUS_AUTH_FAILED = 4
};

struct DisplayData {
    float    weeklyPct;       // 0.0–1.0
    float    sessionPct;      // 0.0–1.0
    String   weeklyReset;     // e.g. "3d 1h"
    String   sessionReset;    // e.g. "2h 15m"
    int      sessions;
    ClaudeStatus status;
    bool     connected;       // companion reachable
    bool     authFailed;      // companion rejected auth secret
};

extern TFT_eSPI tft;
extern DisplayData displayData;

void displayInit();
void displayUpdate();          // full redraw
void displayUpdateClock();     // clock-only partial update (called every second)
String generateScreenSvg();    // SVG representation of current display state
