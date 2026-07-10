#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

// Agent logos arrive over the wire (config message) as 48x48 1-bit bitmaps.
#define LOGO_W 48
#define LOGO_H 48
#define LOGO_BYTES (LOGO_W * LOGO_H / 8)
#define MAX_AGENT_LOGOS 6

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
    String   weeklyTitle;     // e.g. "Claude Weekly"
    String   sessionTitle;    // e.g. "Claude Session"
    String   agentDisplay;    // e.g. "Claude"
    String   agentId;         // e.g. "claude"
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

// Wire-delivered agent logos, used by the sleep screen.
void displayClearAgentLogos();
bool displayAddAgentLogo(uint16_t color565, const uint8_t* bits /*LOGO_BYTES*/);

// Sleep screen: clock + a random selection of agent logos bouncing around.
void displaySleepStart();
void displaySleepTick(unsigned long now);  // call every loop(); paces itself
void displayWake();                        // exit sleep + full redraw
bool displaySleeping();
