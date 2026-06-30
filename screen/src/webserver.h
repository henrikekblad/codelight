#pragma once
#include <ESPAsyncWebServer.h>
#include <Arduino.h>

void webserverInit(AsyncWebServer& server);
void dbgLog(const String& msg);
