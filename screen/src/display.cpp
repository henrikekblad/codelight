#include "display.h"
#include "logo.h"

TFT_eSPI tft = TFT_eSPI();
DisplayData displayData = {0, 0, "--", "--", 0, STATUS_OFFLINE, false, false};

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
    tft.setCursor(X_MARGIN, labelY);
    tft.print(label);
    int labelRight = tft.getCursorX();
    int resetX = 240 - X_MARGIN - tft.textWidth(resetStr);
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

static void drawStatusBox(ClaudeStatus status, bool connected, bool authFailed) {
    uint16_t color;
    const char* label;
    if (authFailed) {
        color = COL_RED; label = "AUTH FAIL";
    } else if (!connected) {
        color = COL_OFFLINE; label = "OFFLINE";
    } else switch (status) {
        case STATUS_WORKING:  color = COL_ORANGE; label = "WORKING";  break;
        case STATUS_WAITING:  color = COL_RED;    label = "WAITING";  break;
        case STATUS_INACTIVE: color = COL_GREEN;  label = "IDLE";     break;
        case STATUS_AUTH_FAILED: color = COL_RED; label = "AUTH FAIL"; break;
        default:              color = COL_OFFLINE; label = "OFFLINE"; break;
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

static DisplayData prev = {-1.0f, -1.0f, "", "", -1, (ClaudeStatus)-1, false, false};
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

    if (displayData.weeklyPct != prev.weeklyPct || displayData.weeklyReset != prev.weeklyReset)
        drawMeterBlock(Y_WMETER, "Weekly", displayData.weeklyPct, displayData.weeklyReset);

    if (displayData.sessionPct != prev.sessionPct || displayData.sessionReset != prev.sessionReset)
        drawMeterBlock(Y_SMETER, "Session", displayData.sessionPct, displayData.sessionReset);

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
        displayData.authFailed != prev.authFailed)
        drawStatusBox(displayData.status, displayData.connected, displayData.authFailed);

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

// ── Sleep screen: bouncing logo ───────────────────────────────────────────────
//
// Two 1-bit sprites (colors applied at push time via setBitmapColor): logo and
// clock both drift DVD-style, bounce on walls, and collide with each other.
// Backlight is PWM-dimmed.
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

static TFT_eSprite logoSpr(&tft);
static TFT_eSprite clkSpr(&tft);
static bool sleeping   = false;
static bool sleepAnim  = false;   // sprites allocated, animation running
static int  lx, ly;               // integer draw position (also used by the SVG preview)
static float fx, fy, vx, vy;      // exact position / velocity
static int  cx, cy;               // clock integer draw position (also for SVG preview)
static float cfx, cfy, cvx, cvy;  // clock exact position / velocity
static int  clkW = 96;
static int  clkH = 48;
static unsigned long lastFrameMs = 0;
static int lastClkMin = -1;

bool displaySleeping() { return sleeping; }

// Pick a fresh speed at a random 25°–65° angle, keeping the current direction
// signs — steep/shallow extremes (edge-scrubbing, pure diagonals) are excluded
static void sleepRandomizeVelocity() {
    float ang = (25.0f + (int)(RANDOM_REG32 % 41)) * (PI / 180.0f);
    vx = (vx < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * cosf(ang);
    vy = (vy < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * sinf(ang);
}

static void sleepRandomizeVelocity(float& svx, float& svy) {
    float ang = (25.0f + (int)(RANDOM_REG32 % 41)) * (PI / 180.0f);
    svx = (svx < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * cosf(ang);
    svy = (svy < 0 ? -1.0f : 1.0f) * SLEEP_SPEED * sinf(ang);
}

static bool sleepOverlap(float ax, float ay, int aw, int ah,
                         float bx, float by, int bw, int bh) {
    return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
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

    // Random start position and direction (hardware RNG)
    fx = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - LOGO_W - 2 * SLEEP_STEP)));
    fy = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - LOGO_H - 2 * SLEEP_STEP)));
    vx = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    vy = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    sleepRandomizeVelocity();

    // Derive the larger clock bounds from the active font metrics.
    tft.setTextFont(2);
    tft.setTextSize(SLEEP_CLK_SCALE);
    clkW = tft.textWidth("00:00") + 4;
    clkH = tft.fontHeight(2);

    // Random clock position, avoiding immediate overlap with the logo.
    bool placed = false;
    for (int tries = 0; tries < 24; tries++) {
        cfx = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - clkW - 2 * SLEEP_STEP)));
        cfy = (float)(SLEEP_STEP + (int)(RANDOM_REG32 % (240 - clkH - 2 * SLEEP_STEP)));
        if (!sleepOverlap(fx, fy, LOGO_W, LOGO_H, cfx, cfy, clkW, clkH)) {
            placed = true;
            break;
        }
    }
    if (!placed) {
        cfx = (fx < 120.0f) ? (float)(240 - clkW - SLEEP_STEP) : (float)SLEEP_STEP;
        cfy = (fy < 120.0f) ? (float)(240 - clkH - SLEEP_STEP) : (float)SLEEP_STEP;
    }
    cvx = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    cvy = (RANDOM_REG32 & 1) ? 1.0f : -1.0f;
    sleepRandomizeVelocity(cvx, cvy);

    lx = (int)fx;
    ly = (int)fy;
    cx = (int)cfx;
    cy = (int)cfy;

    logoSpr.setColorDepth(1);
    clkSpr.setColorDepth(1);
    sleepAnim = logoSpr.createSprite(LOGO_W + 2 * SLEEP_STEP, LOGO_H + 2 * SLEEP_STEP) != nullptr
             && clkSpr.createSprite(clkW + 2 * SLEEP_STEP, clkH + 2 * SLEEP_STEP) != nullptr;

    if (!sleepAnim) {   // heap too tight for sprites: static logo instead
        logoSpr.deleteSprite();
        clkSpr.deleteSprite();
        tft.drawBitmap(lx, ly, LOGO_BITS, LOGO_W, LOGO_H, COL_LOGO);
        return;
    }

    logoSpr.setBitmapColor(COL_LOGO, COL_BG);
    clkSpr.setBitmapColor(COL_GREEN, COL_BG);

    lastFrameMs = 0;
    lastClkMin  = -1;
    renderSleepClock();
}

void displaySleepTick(unsigned long now) {
    if (!sleeping || !sleepAnim || now - lastFrameMs < SLEEP_FRAME_MS) return;
    lastFrameMs = now;

    float prevFx = fx, prevFy = fy;
    float prevCfx = cfx, prevCfy = cfy;

    fx += vx;
    fy += vy;
    cfx += cvx;
    cfy += cvy;

    bool hitX = false, hitY = false;
    if (fx <= 0.0f)                { fx = 0.0f;                vx =  fabsf(vx); hitX = true; }
    else if (fx >= 240 - LOGO_W)   { fx = 240 - LOGO_W;        vx = -fabsf(vx); hitX = true; }
    if (fy <= 0.0f)                { fy = 0.0f;                vy =  fabsf(vy); hitY = true; }
    else if (fy >= 240 - LOGO_H)   { fy = 240 - LOGO_H;        vy = -fabsf(vy); hitY = true; }
    if (hitX || hitY) sleepRandomizeVelocity();   // never settle into an orbit

    bool clkHitX = false, clkHitY = false;
    if (cfx <= 0.0f)               { cfx = 0.0f;               cvx =  fabsf(cvx); clkHitX = true; }
    else if (cfx >= 240 - clkW)    { cfx = 240 - clkW;         cvx = -fabsf(cvx); clkHitX = true; }
    if (cfy <= 0.0f)               { cfy = 0.0f;               cvy =  fabsf(cvy); clkHitY = true; }
    else if (cfy >= 240 - clkH)    { cfy = 240 - clkH;         cvy = -fabsf(cvy); clkHitY = true; }
    if (clkHitX || clkHitY) sleepRandomizeVelocity(cvx, cvy);

    const float lfx = fx + LOGO_COLLIDE_INSET_X;
    const float lfy = fy + LOGO_COLLIDE_INSET_Y;
    const int lfw = LOGO_W - 2 * LOGO_COLLIDE_INSET_X;
    const int lfh = LOGO_H - 2 * LOGO_COLLIDE_INSET_Y;

    const float cox = cfx + CLOCK_COLLIDE_INSET_X;
    const float coy = cfy + CLOCK_COLLIDE_INSET_Y;
    const int cow = clkW - 2 * CLOCK_COLLIDE_INSET_X;
    const int coh = clkH - 2 * CLOCK_COLLIDE_INSET_Y;

    if (sleepOverlap(lfx, lfy, lfw, lfh, cox, coy, cow, coh)) {
        float ox = min(lfx + lfw, cox + cow) - max(lfx, cox);
        float oy = min(lfy + lfh, coy + coh) - max(lfy, coy);
        float dx = (lfx + lfw * 0.5f) - (cox + cow * 0.5f);
        float dy = (lfy + lfh * 0.5f) - (coy + coh * 0.5f);
        bool collideX = (ox < oy);
        if (ox == oy) {
            // Tie-break with dominant movement axis from this frame.
            float mvx = fabsf((fx - prevFx) - (cfx - prevCfx));
            float mvy = fabsf((fy - prevFy) - (cfy - prevCfy));
            collideX = mvx >= mvy;
        }

        if (collideX) {
            float push = (ox * 0.5f) + 0.1f;
            if (dx < 0.0f) { fx -= push; cfx += push; }
            else           { fx += push; cfx -= push; }
            vx = -vx;
            cvx = -cvx;
        } else {
            float push = (oy * 0.5f) + 0.1f;
            if (dy < 0.0f) { fy -= push; cfy += push; }
            else           { fy += push; cfy -= push; }
            vy = -vy;
            cvy = -cvy;
        }

        fx = constrain(fx, 0.0f, (float)(240 - LOGO_W));
        fy = constrain(fy, 0.0f, (float)(240 - LOGO_H));
        cfx = constrain(cfx, 0.0f, (float)(240 - clkW));
        cfy = constrain(cfy, 0.0f, (float)(240 - clkH));
    }

    lx = (int)fx;
    ly = (int)fy;
    cx = (int)cfx;
    cy = (int)cfy;

    // Sprite margin covers the previous position, so one push erases and draws
    logoSpr.fillSprite(0);
    logoSpr.setBitmapColor(COL_LOGO, COL_BG);
    logoSpr.drawBitmap(SLEEP_STEP, SLEEP_STEP, LOGO_BITS, LOGO_W, LOGO_H, 1);
    logoSpr.pushSprite(lx - SLEEP_STEP, ly - SLEEP_STEP);

    renderSleepClock();
    clkSpr.setBitmapColor(COL_GREEN, COL_BG);
    clkSpr.pushSprite(cx - SLEEP_STEP, cy - SLEEP_STEP);
}

static void appendSleepSvg(String& s) {
    s.reserve(s.length() + 1400);
    s += "<path transform='translate("; s += lx; s += ","; s += ly;
    s += ") scale(0.96)' fill='#DE7356' d='"; s += FPSTR(LOGO_PATH); s += "'/>";

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
        logoSpr.deleteSprite();
        clkSpr.deleteSprite();
        sleepAnim = false;
    }

    analogWrite(TFT_BL, 0);      // constant LOW = backlight fully on
    tft.fillScreen(COL_BG);
    drawChrome();
    prev = {-1.0f, -1.0f, "", "", -1, (ClaudeStatus)-1, false, false};
    displayUpdate();
    displayUpdateClock();
}
