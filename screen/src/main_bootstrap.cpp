#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <Updater.h>

static const char* AP_SSID = "codelight-screen-setup";
static ESP8266WebServer server(80);

static const char INDEX_HTML[] PROGMEM = R"(
<!doctype html><html><body>
<h2>codelight bootstrap v8</h2>
<form method='POST' action='/flash' enctype='multipart/form-data'>
  <input type='file' name='fw'>
  <input type='submit' value='Flash'>
</form>
</body></html>
)";

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n\ncodelight bootstrap v8");

    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, nullptr, 6);
    Serial.printf("AP: %s  ip=%s\n", AP_SSID, WiFi.softAPIP().toString().c_str());

    server.on("/", HTTP_GET, []() {
        server.send_P(200, "text/html", INDEX_HTML);
    });

    server.on("/flash", HTTP_POST,
        []() {
            if (Update.hasError()) {
                server.sendHeader("Connection", "close");
                server.send(200, "text/plain",
                    String("FAIL: ") + Update.getErrorString());
                Serial.printf("Updater error: %s\n", Update.getErrorString().c_str());
                return;
            }
            server.sendHeader("Connection", "close");
            server.send(200, "text/plain", "OK — copying firmware directly, rebooting");
            server.client().flush();
            server.client().stop();
            delay(100);
            // The Arduino eboot (runs from RAM, not flash) will copy the staged
            // firmware from 0x100000 to 0x0000 on this restart — no cache issues.
            ESP.restart();
        },
        []() {
            HTTPUpload& up = server.upload();
            if (up.status == UPLOAD_FILE_START) {
                Serial.printf("OTA start: %s\n", up.filename.c_str());
                // Force staging at exactly 0x100000:
                //   stagingStart = FS_start(0x200000) - roundUp(0x100000) = 0x100000
                // Default getFreeSketchSpace() stages at ~0x4F000 which overlaps
                // the copy destination (0x0000–0x99870) and corrupts the eboot.
                if (!Update.begin(0x100000)) {
                    Serial.printf("Update.begin failed: %s\n",
                                  Update.getErrorString().c_str());
                }
            } else if (up.status == UPLOAD_FILE_WRITE) {
                if (Update.write(up.buf, up.currentSize) != up.currentSize) {
                    Serial.printf("Update.write failed: %s\n",
                                  Update.getErrorString().c_str());
                }
            } else if (up.status == UPLOAD_FILE_END) {
                if (Update.end(true)) {
                    Serial.printf("Staged %u bytes at 0x100000\n", up.totalSize);
                } else {
                    Serial.printf("Update.end failed: %s\n",
                                  Update.getErrorString().c_str());
                }
            }
        }
    );

    server.begin();
    Serial.println("Ready — http://192.168.4.1/");
}

void loop() {
    server.handleClient();
}
