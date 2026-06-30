#include "display.h"

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

void displayInit() {
    tft.init();
    tft.setRotation(0);
    tft.setSwapBytes(true);
    tft.fillScreen(COL_BG);
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, LOW);   // active LOW = backlight on

    // Static elements — drawn once, never change
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_TITLE, COL_BG);
    tft.setCursor(X_MARGIN, Y_TITLE);
    tft.print("codelight");
    tft.drawFastHLine(0, Y_DIVIDER, 240, COL_BAR_BG);
}

void displayUpdate() {
    static DisplayData prev = {-1.0f, -1.0f, "", "", -1, (ClaudeStatus)-1, false, false};

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

String generateScreenSvg() {
    String s;
    s.reserve(1500);

    s  = "<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'>";
    s += "<rect width='240' height='240' fill='#000'/>";

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
