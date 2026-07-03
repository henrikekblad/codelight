#include "syncota.h"
#include <ESP8266WebServer.h>
#include <ESP8266HTTPUpdateServer.h>

static ESP8266WebServer        syncServer(81);
static ESP8266HTTPUpdateServer httpUpdater;

void syncOtaInit() {
    httpUpdater.setup(&syncServer);
    syncServer.begin();
}

void syncOtaLoop() {
    syncServer.handleClient();
}
