#pragma once
#include <ESPAsyncWebServer.h>
#include <Arduino.h>

void webserverInit(AsyncWebServer& server);
void webserverLoop();   // drives the synchronous :81 updater; call every loop()
void dbgLog(const String& msg);
