#include "display.h"

TFT_eSPI tft = TFT_eSPI();
DisplayData displayData = {0, 0, "--", "--", 0, STATUS_OFFLINE, false};

// Palette
#define COL_BG       TFT_BLACK
#define COL_TITLE    0xFFFF
#define COL_LABEL    0xC618   // light grey
#define COL_BAR_BG   0x2104   // dark grey
#define COL_BAR_WEEK 0x051F   // blue
#define COL_BAR_SESS 0x07E0   // green
#define COL_RESET    0x8410   // dim grey
#define COL_GREEN    0x0600   // #00C800
#define COL_ORANGE   0xFC40   // #FF8C00
#define COL_RED      0xF840   // #FF2200
#define COL_OFFLINE  0x4208   // dim grey

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
                           uint16_t barColor, const String& resetStr) {
    int barY = labelY + H_LABEL + 2;
    int barX = X_MARGIN;
    int barW = 240 - X_MARGIN * 2 - 30;   // leaves room for "100%"

    // Row 1: label (left) + reset countdown (right)
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_LABEL, COL_BG);
    tft.setCursor(X_MARGIN, labelY);
    tft.print(label);
    tft.setTextColor(COL_RESET, COL_BG);
    tft.setCursor(240 - X_MARGIN - tft.textWidth(resetStr), labelY);
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

static void drawStatusBox(ClaudeStatus status, bool connected) {
    uint16_t color;
    const char* label;
    if (!connected) {
        color = COL_OFFLINE; label = "OFFLINE";
    } else switch (status) {
        case STATUS_WORKING:  color = COL_ORANGE; label = "WORKING";  break;
        case STATUS_WAITING:  color = COL_RED;    label = "WAITING";  break;
        case STATUS_INACTIVE: color = COL_GREEN;  label = "IDLE";     break;
        default:              color = COL_OFFLINE; label = "OFFLINE"; break;
    }

    int bx = (240 - BOX_SIZE) / 2;
    tft.fillRect(bx, Y_BOX, BOX_SIZE, BOX_SIZE, color);

    tft.setTextFont(4);
    tft.setTextSize(1);
    tft.setTextColor(TFT_WHITE, color);
    int tw = tft.textWidth(label);
    int th = tft.fontHeight(4);
    tft.setCursor(bx + (BOX_SIZE - tw) / 2, Y_BOX + (BOX_SIZE - th) / 2);
    tft.print(label);
}

void displayInit() {
    tft.init();
    tft.setRotation(0);
    tft.setSwapBytes(true);
    tft.fillScreen(COL_BG);
    pinMode(TFT_BL, OUTPUT);
    digitalWrite(TFT_BL, LOW);   // active LOW = backlight on
}

void displayUpdate() {
    tft.fillScreen(COL_BG);

    // Title + clock
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_TITLE, COL_BG);
    tft.setCursor(X_MARGIN, Y_TITLE);
    tft.print("CLAUDE CODE");
    displayUpdateClock();

    // Meter blocks
    drawMeterBlock(Y_WMETER, "Weekly",  displayData.weeklyPct,  COL_BAR_WEEK, displayData.weeklyReset);
    drawMeterBlock(Y_SMETER, "Session", displayData.sessionPct, COL_BAR_SESS, displayData.sessionReset);

    // Session count
    tft.setTextFont(2);
    tft.setTextSize(1);
    tft.setTextColor(COL_LABEL, COL_BG);
    tft.setCursor(X_MARGIN, Y_SESSIONS);
    char sbuf[24];
    snprintf(sbuf, sizeof(sbuf), "%d session%s active",
             displayData.sessions, displayData.sessions == 1 ? "" : "s");
    tft.print(sbuf);

    // Divider
    tft.drawFastHLine(0, Y_DIVIDER, 240, COL_BAR_BG);

    drawStatusBox(displayData.status, displayData.connected);
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
