#pragma once

// Synchronous firmware updater on :81 (ESP8266HTTPUpdateServer).
//
// The async (:80) stack on ESP8266 is unreliable for large uploads — AsyncTCP
// window updates stall on an otherwise-quiet device and the transfer dies —
// while this classic blocking updater is the same one the stock firmware
// uses and is rock solid. Blocking in handleClient() also quiesces the rest
// of the firmware for free during the update.
//
// Lives in its own translation unit: ESP8266WebServer and ESPAsyncWebServer
// both define HTTP_GET etc. and cannot be included together.
void syncOtaInit();
void syncOtaLoop();
