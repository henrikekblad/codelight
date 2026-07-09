#include "display.h"
#include "logo.h"

TFT_eSPI tft = TFT_eSPI();
DisplayData displayData = {
    0, 0,
    "--", "--",
    "Claude Weekly", "Claude Session",
    "Claude", "claude",
    0, STATUS_OFFLINE, false, false
};

// Palette
#define COL_BG       TFT_BLACK
#define COL_TITLE    0xFFFF
#define COL_LABEL    0xC618   // light grey
#define COL_BAR_BG   0x2104   // dark grey
#define COL_RESET    0x8410   // dim grey
#define COL_GREEN    0x0600   // #00C800
#define COL_YELLOW   0xFFE0   // #FFFF00
#define COL_ORANGE   0xFC40   // #FF8C00
#define COL_RED      0xF840   // #FF2200
#define COL_OFFLINE  0x4208   // dim grey
#define COL_LOGO     0xDB8A   // #DE7356 Claude terracotta
#define COL_COPILOT  0x051F   // #007FFF
#define COL_CODEX    0xFFFF   // white

// Linearly interpolate between two RGB565 colours (t in 0..1).
static uint16_t lerpColor565(uint16_t c0, uint16_t c1, float t) {
    int r0 = (c0 >> 11) & 0x1F, r1 = (c1 >> 11) & 0x1F;
    int g0 = (c0 >>  5) & 0x3F, g1 = (c1 >>  5) & 0x3F;
    int b0 =  c0        & 0x1F, b1 =  c1        & 0x1F;
    return (uint16_t)(((int)(r0 + t * (r1 - r0)) << 11) |
                      ((int)(g0 + t * (g1 - g0)) <<  5) |
                       (int)(b0 + t * (b1 - b0)));
}

// Map usage percentage to a colour: green → yellow → orange → red.
static uint16_t usageColor(float pct) {
    static const uint16_t stops[4] = { COL_GREEN, COL_YELLOW, COL_ORANGE, COL_RED };
    static const float    edges[4] = { 0.0f, 0.5f, 0.75f, 1.0f };
    if (pct <= 0.0f) return stops[0];
    if (pct >= 1.0f) return stops[3];
    for (int i = 0; i < 3; i++) {
        if (pct <= edges[i + 1]) {
            float t = (pct - edges[i]) / (edges[i + 1] - edges[i]);
            return lerpColor565(stops[i], stops[i + 1], t);
        }
    }
    return stops[3];
}

// Layout (240×240)
#define X_MARGIN     6
#define Y_TITLE      2
#define H_LABEL      16   // font2 row height
#define H_BAR        20   // bar height (wider bar allows taller)

#define Y_WMETER     22                           // weekly: label+reset row
#define Y_WBAR       (Y_WMETER + H_LABEL + 2)    // 40
#define Y_SMETER     (Y_WBAR + H_BAR + 5)        // 65
#define Y_SBAR       (Y_SMETER + H_LABEL + 2)    // 83
#define Y_SESSIONS   (Y_SBAR + H_BAR + 4)        // 107
#define Y_DIVIDER    (Y_SESSIONS + H_LABEL + 3)  // 126
#define Y_BOX        (Y_DIVIDER + 2)             // 128
#define BOX_SIZE     (240 - Y_BOX - 4)           // 108

// Two-row meter: "Label    ⟳ reset" on first row, full-width bar on second.
static void drawMeterBlock(int labelY, const char* label, float pct,
                           const String& resetStr) {
    uint16_t barColor = usageColor(pct);
    int barY = labelY + H_LABEL + 2;
    int barX = X_MARGIN;
    int barW = 240 - X_MARGIN * 2 - 30;   // leaves room for "100%"

    // Row 1: label (left) + reset countdown (right)
    // Text renderer fills its own character backgrounds; only clear the gap between them.
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_LABEL, COL_BG);

    int resetW = tft.textWidth(resetStr);
    int resetX = 240 - X_MARGIN - resetW;
    int maxLabelW = resetX - X_MARGIN - 4;   // leave a small visual gap

    String labelText = label ? String(label) : String("");
    if (maxLabelW < 0) maxLabelW = 0;
    if (tft.textWidth(labelText) > maxLabelW) {
        const String dots = "...";
        while (labelText.length() > 0 && tft.textWidth(labelText + dots) > maxLabelW)
            labelText.remove(labelText.length() - 1);
        labelText += dots;
    }

    tft.setCursor(X_MARGIN, labelY);
    tft.print(labelText);
    int labelRight = tft.getCursorX();
    if (resetX > labelRight)
        tft.fillRect(labelRight, labelY, resetX - labelRight, H_LABEL, COL_BG);
    tft.setTextColor(COL_RESET, COL_BG);
    tft.setCursor(resetX, labelY);
    tft.print(resetStr);

    // Row 2: bar + percentage
    tft.fillRect(barX, barY, barW, H_BAR, COL_BAR_BG);
    int filled = constrain((int)(pct * barW), 0, barW);
    if (filled > 0) tft.fillRect(barX, barY, filled, H_BAR, barColor);

    char buf[6];
    snprintf(buf, sizeof(buf), "%3d%%", (int)(pct * 100));
    tft.setTextColor(COL_TITLE, COL_BG);
    tft.setCursor(240 - X_MARGIN - 28, barY + 2);
    tft.print(buf);
}

static void drawStatusBox(ClaudeStatus status, bool connected, bool authFailed,
                          const String& agentDisplay) {
    uint16_t color;
    const char* stateLabel;
    if (authFailed) {
        color = COL_RED; stateLabel = "AUTH FAIL";
    } else if (!connected) {
        color = COL_OFFLINE; stateLabel = "OFFLINE";
    } else switch (status) {
        case STATUS_WORKING:  color = COL_ORANGE; stateLabel = "WORKING";  break;
        case STATUS_WAITING:  color = COL_RED;    stateLabel = "WAITING";  break;
        case STATUS_INACTIVE: color = COL_GREEN;  stateLabel = "IDLE";     break;
        case STATUS_AUTH_FAILED: color = COL_RED; stateLabel = "AUTH FAIL"; break;
        default:              color = COL_OFFLINE; stateLabel = "OFFLINE"; break;
    }

    String label = agentDisplay;
    if (!connected || authFailed) {
        label = String(stateLabel);
    } else {
        label.toUpperCase();
        label += " ";
        label += stateLabel;
    }

    tft.fillRect(0, Y_BOX, 240, BOX_SIZE, color);

    tft.setTextFont(4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_BLACK, color);
    int tw = tft.textWidth(label);
    int th = tft.fontHeight(4);
    tft.setCursor((240 - tw) / 2, Y_BOX + (BOX_SIZE - th) / 2);
    tft.print(label);
}

// Static elements — drawn on init and after waking from the sleep screen
static void drawChrome() {
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_TITLE, COL_BG);
    tft.setCursor(X_MARGIN, Y_TITLE);
    tft.print("codelight");
    tft.drawFastHLine(0, Y_DIVIDER, 240, COL_BAR_BG);
}

static bool displayUsable = false;   // displayInit() completed (not crash-guard skipped)

void displayInit() {
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, HIGH);  // backlight OFF while we initialise (active LOW)
    tft.init();
    tft.setRotation(0);
    tft.setSwapBytes(true);
    tft.fillScreen(COL_BG);
    digitalWrite(TFT_BL, LOW);   // backlight ON — screen is already black

    drawChrome();
    displayUsable = true;
}

static DisplayData prev = {
    -1.0f, -1.0f,
    "", "",
    "", "",
    "", "",
    -1, (ClaudeStatus)-1, false, false
};
static bool firstUpdate = true;

void displayUpdate() {
    if (displaySleeping()) return;

    // On the very first call, do a full clear to remove any residual content
    // in the small gaps between drawn regions (margins, inter-row spacing).
    // After that only dirty regions are redrawn so this never runs again.
    if (firstUpdate) {
        tft.fillScreen(COL_BG);
        firstUpdate = false;
    }

    // Always redraw chrome — showWifiStatus() and sleep-wake both do a full
    // fillScreen that erases the title and divider.
    drawChrome();

    if (displayData.weeklyPct != prev.weeklyPct
        || displayData.weeklyReset != prev.weeklyReset
        || displayData.weeklyTitle != prev.weeklyTitle)
        drawMeterBlock(Y_WMETER, displayData.weeklyTitle.c_str(),
                       displayData.weeklyPct, displayData.weeklyReset);

    if (displayData.sessionPct != prev.sessionPct
        || displayData.sessionReset != prev.sessionReset
        || displayData.sessionTitle != prev.sessionTitle)
        drawMeterBlock(Y_SMETER, displayData.sessionTitle.c_str(),
                       displayData.sessionPct, displayData.sessionReset);

    if (displayData.sessions != prev.sessions) {
        tft.setTextFont(2);
        tft.setTextSize(1);
        tft.setTextColor(COL_LABEL, COL_BG);
        tft.setCursor(X_MARGIN, Y_SESSIONS);
        char sbuf[24];
        snprintf(sbuf, sizeof(sbuf), "%d session%s active",
                 displayData.sessions, displayData.sessions == 1 ? "" : "s");
        tft.print(sbuf);
        tft.fillRect(tft.getCursorX(), Y_SESSIONS, 240 - tft.getCursorX(), H_LABEL, COL_BG);
    }

    if (displayData.status != prev.status || displayData.connected != prev.connected ||
        displayData.authFailed != prev.authFailed ||
        displayData.agentDisplay != prev.agentDisplay)
        drawStatusBox(displayData.status, displayData.connected,
                      displayData.authFailed, displayData.agentDisplay);

    prev = displayData;

    displayUpdateClock();
}

// ── SVG screen dump ───────────────────────────────────────────────────────────

static String rgb888Hex(uint8_t r, uint8_t g, uint8_t b) {
    char buf[8];
    snprintf(buf, sizeof(buf), "#%02x%02x%02x", r, g, b);
    return String(buf);
}

static String usageColorHex(float pct) {
    struct Stop { float edge; uint8_t r, g, b; };
    static const Stop stops[4] = {
        {0.00f,   0, 200,   0},
        {0.50f, 255, 255,   0},
        {0.75f, 255, 140,   0},
        {1.00f, 255,  34,   0},
    };
    if (pct <= 0.0f) return rgb888Hex(stops[0].r, stops[0].g, stops[0].b);
    if (pct >= 1.0f) return rgb888Hex(stops[3].r, stops[3].g, stops[3].b);
    for (int i = 0; i < 3; i++) {
        if (pct <= stops[i+1].edge) {
            float t = (pct - stops[i].edge) / (stops[i+1].edge - stops[i].edge);
            return rgb888Hex(
                (uint8_t)(stops[i].r + t*(stops[i+1].r - stops[i].r)),
                (uint8_t)(stops[i].g + t*(stops[i+1].g - stops[i].g)),
                (uint8_t)(stops[i].b + t*(stops[i+1].b - stops[i].b)));
        }
    }
    return "#ff2200";
}

static void svgText(String& s, int x, int y, const char* fill, int sz,
                    const char* anchor, const String& text) {
    s += "<text x='"; s += x; s += "' y='"; s += y;
    s += "' fill='"; s += fill;
    s += "' font-family='monospace' font-size='"; s += sz; s += "'";
    if (anchor) { s += " text-anchor='"; s += anchor; s += "'"; }
    s += '>'; s += text; s += "</text>";
}

static void svgMeter(String& s, int labelY, const char* label, float pct,
                     const String& resetStr) {
    int barY  = labelY + H_LABEL + 2;
    int barW  = 240 - X_MARGIN * 2 - 30;
    int filled = constrain((int)(pct * barW), 0, barW);

    svgText(s, X_MARGIN,      labelY + 13, "#c0c0c0", 13, nullptr,  label);
    svgText(s, 234,           labelY + 13, "#808080", 13, "end",     resetStr);

    s += "<rect x='"; s += X_MARGIN; s += "' y='"; s += barY;
    s += "' width='"; s += barW;     s += "' height='"; s += H_BAR; s += "' fill='#202020'/>";

    if (filled > 0) {
        s += "<rect x='"; s += X_MARGIN; s += "' y='"; s += barY;
        s += "' width='"; s += filled; s += "' height='"; s += H_BAR;
        s += "' fill='"; s += usageColorHex(pct); s += "'/>";
    }

    char buf[6]; snprintf(buf, sizeof(buf), "%d%%", (int)(pct * 100));
    svgText(s, 234, barY + 14, "#ffffff", 13, "end", buf);
}

static void appendSleepSvg(String& s);   // defined with the sleep-screen state below

static uint16_t logoColorForAgent(const char* agentId) {
    if (strcmp(agentId, "copilot") == 0) return COL_COPILOT;
    if (strcmp(agentId, "codex") == 0) return COL_CODEX;
    return COL_LOGO;
}

static const __FlashStringHelper* logoPathForAgent(const char* agentId) {
    if (strcmp(agentId, "copilot") == 0) return FPSTR(COPILOT_LOGO_PATH);
    if (strcmp(agentId, "codex") == 0) return FPSTR(CODEX_LOGO_PATH);
    return FPSTR(LOGO_PATH);
}

static const uint8_t* logoBitsForAgent(const char* agentId) {
    if (strcmp(agentId, "copilot") == 0) return COPILOT_LOGO_BITS;
    if (strcmp(agentId, "codex") == 0) return CODEX_LOGO_BITS;
    return LOGO_BITS;
}

String generateScreenSvg() {
    String s;
    s.reserve(1500);

    s  = "<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'>";
    s += "<rect width='240' height='240' fill='#000'/>";

    if (displaySleeping()) {
        appendSleepSvg(s);
        s += "</svg>";
        return s;
    }

    // Title + clock
    svgText(s, X_MARGIN, Y_TITLE + 13, "#ffffff", 13, nullptr, "codelight");
    time_t now = time(nullptr);
    struct tm* tm_ = localtime(&now);
    char clk[10];
    snprintf(clk, sizeof(clk), "%02d:%02d:%02d", tm_->tm_hour, tm_->tm_min, tm_->tm_sec);
    svgText(s, 234, Y_TITLE + 13, "#ffffff", 13, "end", clk);

    // Meter bars
    svgMeter(s, Y_WMETER, "Weekly",  displayData.weeklyPct,  displayData.weeklyReset);
    svgMeter(s, Y_SMETER, "Session", displayData.sessionPct, displayData.sessionReset);

    // Session count
    char sbuf[24];
    snprintf(sbuf, sizeof(sbuf), "%d session%s active",
             displayData.sessions, displayData.sessions == 1 ? "" : "s");
    svgText(s, X_MARGIN, Y_SESSIONS + 13, "#c0c0c0", 13, nullptr, sbuf);

    // Divider
    s += "<line x1='0' y1='"; s += Y_DIVIDER;
    s += "' x2='240' y2='"; s += Y_DIVIDER; s += "' stroke='#202020'/>";

    // Status box
    const char* sc; const char* sl;
    if (displayData.authFailed) {
        sc = "#ff2200"; sl = "AUTH FAIL";
    } else if (!displayData.connected) {
        sc = "#404040"; sl = "OFFLINE";
    } else switch (displayData.status) {
        case STATUS_WORKING:  sc = "#ff8c00"; sl = "WORKING";  break;
        case STATUS_WAITING:  sc = "#ff2200"; sl = "WAITING";  break;
        case STATUS_INACTIVE: sc = "#00c800"; sl = "IDLE";     break;
        default:              sc = "#404040"; sl = "OFFLINE";  break;
    }
    s += "<rect x='0' y='"; s += Y_BOX;
    s += "' width='240' height='"; s += BOX_SIZE;
    s += "' fill='"; s += sc; s += "'/>";
    s += "<text x='120' y='"; s += (Y_BOX + BOX_SIZE/2 + 8);
    s += "' fill='#000' font-family='sans-serif' font-size='20' font-weight='bold'"
         " text-anchor='middle'>"; s += sl; s += "</text>";

    s += "</svg>";
    return s;
}

void displayUpdateClock() {
    if (displaySleeping()) return;

    time_t now = time(nullptr);
    struct tm* t = localtime(&now);

    char buf[10];
    snprintf(buf, sizeof(buf), "%02d:%02d:%02d", t->tm_hour, t->tm_min, t->tm_sec);

    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_TITLE, COL_BG);
    int tw = tft.textWidth(buf);
    tft.setCursor(240 - X_MARGIN - tw, Y_TITLE);
    tft.print(buf);
}

// ── Sleep screen: bouncing logos + clock ─────────────────────────────────────
//
// Three 1-bit logo sprites (Claude/Copilot/Codex) and one clock sprite bounce
// around simultaneously. Backlight is PWM-dimmed.
//
// Velocity is a float vector at a random angle, re-jittered on every wall
// bounce — with a fixed ±step the path is locked to 45° diagonals and traces
// the same square lattice forever.

#define SLEEP_FRAME_MS  40      // 25 fps
#define SLEEP_SPEED     2.4f    // px per frame
#define SLEEP_STEP      3       // sprite margin; must cover ceil(SLEEP_SPEED)
#define SLEEP_BL_LEVEL  50      // backlight duty while asleep (0-255)
#define SLEEP_CLK_SCALE 3       // 3x the regular sleep clock size
#define LOGO_COLLIDE_INSET_X 0  // tighter hitbox to match visible logo shape
#define LOGO_COLLIDE_INSET_Y 0
#define CLOCK_COLLIDE_INSET_X 2 // trim font-side padding from clock box
#define CLOCK_COLLIDE_INSET_Y 2

static TFT_eSprite claudeLogoSpr(&tft);
static TFT_eSprite copilotLogoSpr(&tft);
static TFT_eSprite codexLogoSpr(&tft);
static TFT_eSprite clkSpr(&tft);
static bool sleeping   = false;
static bool sleepAnim  = false;   // sprites allocated, animation running
struct SleepLogoState {
    const char* id;
    TFT_eSprite* spr;
    float fx, fy, vx, vy;
    int x, y;
    int prevX, prevY;
};
static SleepLogoState sleepLogos[3] = {
    {"claude",  &claudeLogoSpr,  0, 0, 0, 0, 0, 0, 0, 0},
    {"copilot", &copilotLogoSpr, 0, 0, 0, 0, 0, 0, 0, 0},
    {"codex",   &codexLogoSpr,   0, 0, 0, 0, 0, 0, 0, 0},
};
static int  cx, cy;               // clock integer draw position (also for SVG preview)
static float cfx, cfy, cvx, cvy;  // clock exact position / velocity
static int  clkW = 96;
static int  clkH = 48;
static unsigned long lastFrameMs = 0;
static int lastClkMin = -1;

bool displaySleeping() { return sleeping; }

static void sleepInitVelocity(float& svx, float& svy) {
    float ang = (25.0f + (int)(RANDOM_REG32 % 41)) * (PI / 180.0f);
    svx = (svx < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * cosf(ang);
    svy = (svy < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * sinf(ang);
}

static bool sleepOverlap(float ax, float ay, int aw, int ah,
                         float bx, float by, int bw, int bh) {
    return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
}

static void sleepClearOldDelta(int oldX, int oldY, int newX, int newY,
                               int w, int h, uint16_t bg) {
    if (oldX == newX && oldY == newY) return;

    int oldR = oldX + w;
    int oldB = oldY + h;
    int newR = newX + w;
    int newB = newY + h;

    int ix0 = oldX > newX ? oldX : newX;
    int iy0 = oldY > newY ? oldY : newY;
    int ix1 = oldR < newR ? oldR : newR;
    int iy1 = oldB < newB ? oldB : newB;

    // No overlap: clear the entire old rect.
    if (ix0 >= ix1 || iy0 >= iy1) {
        tft.fillRect(oldX, oldY, w, h, bg);
        return;
    }

    // Clear only exposed bands of the old rect not covered by the new rect.
    if (oldY < iy0) tft.fillRect(oldX, oldY, w, iy0 - oldY, bg);      // top
    if (iy1 < oldB) tft.fillRect(oldX, iy1, w, oldB - iy1, bg);       // bottom
    if (oldX < ix0) tft.fillRect(oldX, iy0, ix0 - oldX, iy1 - iy0, bg); // left
    if (ix1 < oldR) tft.fillRect(ix1, iy0, oldR - ix1, iy1 - iy0, bg);  // right
}

static bool sleepOverlapInset(float ax, float ay, int aw, int ah, int aix, int aiy,
                              float bx, float by, int bw, int bh, int bix, int biy) {
    float al = ax + aix;
    float at = ay + aiy;
    float ar = ax + aw - aix;
    float ab = ay + ah - aiy;
    float bl = bx + bix;
    float bt = by + biy;
    float br = bx + bw - bix;
    float bb = by + bh - biy;
    return al < br && ar > bl && at < bb && ab > bt;
}

static void sleepResolveCollision(float& ax, float& ay, float& avx, float& avy, int aw, int ah, int aix, int aiy,
                                  float& bx, float& by, float& bvx, float& bvy, int bw, int bh, int bix, int biy) {
    float al = ax + aix;
    float at = ay + aiy;
    float ar = ax + aw - aix;
    float ab = ay + ah - aiy;
    float bl = bx + bix;
    float bt = by + biy;
    float br = bx + bw - bix;
    float bb = by + bh - biy;

    float overlapX = fminf(ar, br) - fmaxf(al, bl);
    float overlapY = fminf(ab, bb) - fmaxf(at, bt);
    if (overlapX <= 0.0f || overlapY <= 0.0f) return;

    // Equal-mass elastic collision: swap velocity component on the shallow axis,
    // and separate both bodies to avoid sticking.
    if (overlapX < overlapY) {
        float ta = avx;
        avx = bvx;
        bvx = ta;
        float push = overlapX * 0.5f + 0.05f;
        if ((ax + aw * 0.5f) < (bx + bw * 0.5f)) {
            ax -= push;
            bx += push;
        } else {
            ax += push;
            bx -= push;
        }
    } else {
        float ta = avy;
        avy = bvy;
        bvy = ta;
        float push = overlapY * 0.5f + 0.05f;
        if ((ay + ah * 0.5f) < (by + bh * 0.5f)) {
            ay -= push;
            by += push;
        } else {
            ay += push;
            by -= push;
        }
    }
}

static void renderSleepClock() {
    time_t now = time(nullptr);
    struct tm* t = localtime(&now);
    if (t->tm_min == lastClkMin) return;
    lastClkMin = t->tm_min;

    char buf[8];
    snprintf(buf, sizeof(buf), "%02d:%02d", t->tm_hour, t->tm_min);

    clkSpr.fillSprite(0);
    clkSpr.setTextFont(2);
    clkSpr.setTextSize(SLEEP_CLK_SCALE);
    clkSpr.setBitmapColor(COL_GREEN, COL_BG);
    clkSpr.setTextColor(1);
    int textW = clkSpr.textWidth(buf);
    int textH = clkSpr.fontHeight(2);
    int tx = SLEEP_STEP + (clkW - textW) / 2;
    int ty = SLEEP_STEP + (clkH - textH) / 2;
    clkSpr.setCursor(tx, ty);
    clkSpr.print(buf);
}

void displaySleepStart() {
    if (sleeping || !displayUsable) return;
    sleeping = true;

    tft.fillScreen(COL_BG);
    analogWriteRange(255);
    analogWrite(TFT_BL, 255 - SLEEP_BL_LEVEL);   // active LOW

    // Random start positions and directions for all three logos.
    for (int i = 0; i < 3; i++) {
        bool placed = false;
        for (int tries = 0; tries < 32; tries++) {
            sleepLogos[i].fx = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - LOGO_W - 2 * SLEEP_STEP)));
            sleepLogos[i].fy = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - LOGO_H - 2 * SLEEP_STEP)));
            bool overlaps = false;
            for (int j = 0; j < i; j++) {
                if (sleepOverlap(sleepLogos[i].fx, sleepLogos[i].fy, LOGO_W, LOGO_H,
                                 sleepLogos[j].fx, sleepLogos[j].fy, LOGO_W, LOGO_H)) {
                    overlaps = true;
                    break;
                }
            }
            if (!overlaps) {
                placed = true;
                break;
            }
        }
        if (!placed) {
            sleepLogos[i].fx = (float)(SLEEP_STEP + i * (LOGO_W + 6));
            sleepLogos[i].fy = (float)(SLEEP_STEP + i * 8);
        }
        sleepLogos[i].vx = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
        sleepLogos[i].vy = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
        sleepInitVelocity(sleepLogos[i].vx, sleepLogos[i].vy);
        sleepLogos[i].x = (int)sleepLogos[i].fx;
        sleepLogos[i].y = (int)sleepLogos[i].fy;
        sleepLogos[i].prevX = sleepLogos[i].x;
        sleepLogos[i].prevY = sleepLogos[i].y;
    }

    // Derive the larger clock bounds from the active font metrics.
    tft.setTextFont(2);
    tft.setTextSize(SLEEP_CLK_SCALE);
    clkW = tft.textWidth("00:00") + 4;
    clkH = tft.fontHeight(2);

    // Random clock position, avoiding immediate overlap with logos.
    bool placed = false;
    for (int tries = 0; tries < 24; tries++) {
        cfx = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - clkW - 2 * SLEEP_STEP)));
        cfy = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - clkH - 2 * SLEEP_STEP)));
        bool overlaps = false;
        for (int i = 0; i < 3; i++) {
            if (sleepOverlap(sleepLogos[i].fx, sleepLogos[i].fy, LOGO_W, LOGO_H, cfx, cfy, clkW, clkH)) {
                overlaps = true;
                break;
            }
        }
        if (!overlaps) {
            placed = true;
            break;
        }
    }
    if (!placed) {
        cfx = (float)(240 - clkW - SLEEP_STEP);
        cfy = (float)(240 - clkH - SLEEP_STEP);
    }
    cvx = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    cvy = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    sleepInitVelocity(cvx, cvy);

    cx = (int)cfx;
    cy = (int)cfy;

    // Keep logos in direct-color sprites so each one retains its own tint.
    // 1-bit palette sprites can end up sharing effective bitmap colors.
    claudeLogoSpr.setColorDepth(8);
    copilotLogoSpr.setColorDepth(8);
    codexLogoSpr.setColorDepth(8);
    clkSpr.setColorDepth(1);
    sleepAnim = claudeLogoSpr.createSprite(LOGO_W + 2 * SLEEP_STEP, LOGO_H + 2 * SLEEP_STEP) != nullptr
             && copilotLogoSpr.createSprite(LOGO_W + 2 * SLEEP_STEP, LOGO_H + 2 * SLEEP_STEP) != nullptr
             && codexLogoSpr.createSprite(LOGO_W + 2 * SLEEP_STEP, LOGO_H + 2 * SLEEP_STEP) != nullptr
             && clkSpr.createSprite(clkW + 2 * SLEEP_STEP, clkH + 2 * SLEEP_STEP) != nullptr;

    if (!sleepAnim) {   // heap too tight for sprites: static fallback
        claudeLogoSpr.deleteSprite();
        copilotLogoSpr.deleteSprite();
        codexLogoSpr.deleteSprite();
        clkSpr.deleteSprite();
        for (int i = 0; i < 3; i++) {
            tft.drawBitmap(sleepLogos[i].x, sleepLogos[i].y,
                           logoBitsForAgent(sleepLogos[i].id), LOGO_W, LOGO_H,
                           logoColorForAgent(sleepLogos[i].id));
        }
        return;
    }

    // Logo pixels never change while sleeping. Render each sprite once so the
    // 25 fps hot path only moves prebuilt buffers and performs no allocation.
    for (int i = 0; i < 3; i++) {
        sleepLogos[i].spr->fillSprite(0);
        sleepLogos[i].spr->drawBitmap(
            SLEEP_STEP, SLEEP_STEP, logoBitsForAgent(sleepLogos[i].id),
            LOGO_W, LOGO_H, logoColorForAgent(sleepLogos[i].id));
    }

    clkSpr.setBitmapColor(COL_GREEN, COL_BG);

    lastFrameMs = 0;
    lastClkMin  = -1;
    renderSleepClock();
}

void displaySleepTick(unsigned long now) {
    if (!sleeping || !sleepAnim || now - lastFrameMs < SLEEP_FRAME_MS) return;
    lastFrameMs = now;

    int oldX[3], oldY[3];
    for (int i = 0; i < 3; i++) {
        oldX[i] = sleepLogos[i].x;
        oldY[i] = sleepLogos[i].y;
    }
    int oldCx = cx;
    int oldCy = cy;

    for (int i = 0; i < 3; i++) {
        sleepLogos[i].prevX = sleepLogos[i].x;
        sleepLogos[i].prevY = sleepLogos[i].y;
        sleepLogos[i].fx += sleepLogos[i].vx;
        sleepLogos[i].fy += sleepLogos[i].vy;

        if (sleepLogos[i].fx <= 0.0f) {
            sleepLogos[i].fx = 0.0f;
            sleepLogos[i].vx = fabsf(sleepLogos[i].vx);
        } else if (sleepLogos[i].fx >= 240 - LOGO_W) {
            sleepLogos[i].fx = 240 - LOGO_W;
            sleepLogos[i].vx = -fabsf(sleepLogos[i].vx);
        }
        if (sleepLogos[i].fy <= 0.0f) {
            sleepLogos[i].fy = 0.0f;
            sleepLogos[i].vy = fabsf(sleepLogos[i].vy);
        } else if (sleepLogos[i].fy >= 240 - LOGO_H) {
            sleepLogos[i].fy = 240 - LOGO_H;
            sleepLogos[i].vy = -fabsf(sleepLogos[i].vy);
        }

        sleepLogos[i].x = (int)sleepLogos[i].fx;
        sleepLogos[i].y = (int)sleepLogos[i].fy;
    }

    cfx += cvx;
    cfy += cvy;

    if (cfx <= 0.0f)               { cfx = 0.0f;               cvx =  fabsf(cvx); }
    else if (cfx >= 240 - clkW)    { cfx = 240 - clkW;         cvx = -fabsf(cvx); }
    if (cfy <= 0.0f)               { cfy = 0.0f;               cvy =  fabsf(cvy); }
    else if (cfy >= 240 - clkH)    { cfy = 240 - clkH;         cvy = -fabsf(cvy); }

    cx = (int)cfx;
    cy = (int)cfy;

    // Object-to-object collisions: 3 logos + clock all bounce off each other.
    for (int i = 0; i < 3; i++) {
        for (int j = i + 1; j < 3; j++) {
            if (sleepOverlapInset(
                    sleepLogos[i].fx, sleepLogos[i].fy, LOGO_W, LOGO_H,
                    LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y,
                    sleepLogos[j].fx, sleepLogos[j].fy, LOGO_W, LOGO_H,
                    LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y)) {
                sleepResolveCollision(
                    sleepLogos[i].fx, sleepLogos[i].fy, sleepLogos[i].vx, sleepLogos[i].vy,
                    LOGO_W, LOGO_H, LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y,
                    sleepLogos[j].fx, sleepLogos[j].fy, sleepLogos[j].vx, sleepLogos[j].vy,
                    LOGO_W, LOGO_H, LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y);
            }
        }
    }
    for (int i = 0; i < 3; i++) {
        if (sleepOverlapInset(
                sleepLogos[i].fx, sleepLogos[i].fy, LOGO_W, LOGO_H,
                LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y,
                cfx, cfy, clkW, clkH,
                CLOCK_COLLIDE_INSET_X, CLOCK_COLLIDE_INSET_Y)) {
            sleepResolveCollision(
                sleepLogos[i].fx, sleepLogos[i].fy, sleepLogos[i].vx, sleepLogos[i].vy,
                LOGO_W, LOGO_H, LOGO_COLLIDE_INSET_X, LOGO_COLLIDE_INSET_Y,
                cfx, cfy, cvx, cvy,
                clkW, clkH, CLOCK_COLLIDE_INSET_X, CLOCK_COLLIDE_INSET_Y);
        }
    }

    // Keep objects in bounds after collision separation.
    for (int i = 0; i < 3; i++) {
        sleepLogos[i].fx = constrain(sleepLogos[i].fx, 0.0f, (float)(240 - LOGO_W));
        sleepLogos[i].fy = constrain(sleepLogos[i].fy, 0.0f, (float)(240 - LOGO_H));
        sleepLogos[i].x = (int)sleepLogos[i].fx;
        sleepLogos[i].y = (int)sleepLogos[i].fy;
    }
    cfx = constrain(cfx, 0.0f, (float)(240 - clkW));
    cfy = constrain(cfy, 0.0f, (float)(240 - clkH));
    cx = (int)cfx;
    cy = (int)cfy;

    // Interleave clear+draw per object to avoid a global blank phase.
    // Only clear exposed old->new delta bands to minimize pixel churn.
    for (int i = 0; i < 3; i++) {
        int oldRectX = oldX[i] - SLEEP_STEP;
        int oldRectY = oldY[i] - SLEEP_STEP;
        int newRectX = sleepLogos[i].x - SLEEP_STEP;
        int newRectY = sleepLogos[i].y - SLEEP_STEP;
        int rectW = LOGO_W + 2 * SLEEP_STEP;
        int rectH = LOGO_H + 2 * SLEEP_STEP;

        sleepClearOldDelta(oldRectX, oldRectY, newRectX, newRectY, rectW, rectH, COL_BG);

        sleepLogos[i].spr->pushSprite(sleepLogos[i].x - SLEEP_STEP,
                                      sleepLogos[i].y - SLEEP_STEP);
    }

    renderSleepClock();
    clkSpr.setBitmapColor(COL_GREEN, COL_BG);

    int oldClkRectX = oldCx - SLEEP_STEP;
    int oldClkRectY = oldCy - SLEEP_STEP;
    int newClkRectX = cx - SLEEP_STEP;
    int newClkRectY = cy - SLEEP_STEP;
    int clkRectW = clkW + 2 * SLEEP_STEP;
    int clkRectH = clkH + 2 * SLEEP_STEP;
    sleepClearOldDelta(oldClkRectX, oldClkRectY, newClkRectX, newClkRectY, clkRectW, clkRectH, COL_BG);

    clkSpr.pushSprite(cx - SLEEP_STEP, cy - SLEEP_STEP);
}

static void appendSleepSvg(String& s) {
    s.reserve(s.length() + 1400);
    for (int i = 0; i < 3; i++) {
        const char* aid = sleepLogos[i].id;
        const char* fill = "#DE7356";
        float scale = 0.96f;
        int dy = 0;
        if (strcmp(aid, "copilot") == 0) {
            fill = "#007FFF";
            scale = 0.1875f;
            dy = 4;
        } else if (strcmp(aid, "codex") == 0) {
            fill = "#FFFFFF";
            scale = 0.1846f;
        }
        s += "<path transform='translate(";
        s += sleepLogos[i].x;
        s += ",";
        s += (sleepLogos[i].y + dy);
        s += ") scale(";
        s += String(scale, 4);
        s += ")' fill='";
        s += fill;
        s += "' d='";
        s += logoPathForAgent(aid);
        s += "'/>";
    }

    time_t now = time(nullptr);
    struct tm* t = localtime(&now);
    char buf[8];
    snprintf(buf, sizeof(buf), "%02d:%02d", t->tm_hour, t->tm_min);
    svgText(s, cx + (clkW / 2), cy + clkH - 6, "#00c800", 39, "middle", buf);
}

void displayWake() {
    if (!sleeping) return;
    sleeping = false;

    if (sleepAnim) {
        claudeLogoSpr.deleteSprite();
        copilotLogoSpr.deleteSprite();
        codexLogoSpr.deleteSprite();
        clkSpr.deleteSprite();
        sleepAnim = false;
    }

    analogWrite(TFT_BL, 0);      // constant LOW = backlight fully on
    tft.fillScreen(COL_BG);
    drawChrome();
    prev = {
        -1.0f, -1.0f,
        "", "",
        "", "",
        "", "",
        -1, (ClaudeStatus)-1, false, false
    };
    displayUpdate();
    displayUpdateClock();
}
