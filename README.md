# codelight — Claude Code status display

Live Claude Code dashboard built with five components. Pick and choose whatever suits your needs:

| Component | Description | Example
|---|---|---|
| [**companion/**](companion/README.md) | Python daemon that polls Claude Code usage and pushes it over WebSocket|
| [**screen/**](screen/README.md) | ESP8266 firmware for the GeekMagic Ultra — renders usage bars and status | <img src="assets/demo.jpg" width="600" alt="GeekMagic Ultra showing the codelight IDLE screen"> |
| [**android/**](android/README.md) | Android home-screen widget showing the same data via WebSocket | <img src="assets/android.jpg" width="360" alt="codelight Android widget"> |
| [**gnome-extension/**](gnome-extension/README.md) | GNOME Shell status-bar extension | <img src="assets/gnome-extension.png" width="600" alt="codelight GNOME Shell extension">|
| [**vscode-extension/**](vscode-extension/README.md) | VSCode status-bar extension showing Claude status | |

**Remote permission approval:** with the companion's `--remote-permissions`
flag, Claude Code permission prompts can be approved remotely from the phone
or a GNOME notification — bypassing the built-in dialog (in VSCode you answer
Claude Code's own native dialog). See
[companion/README.md](companion/README.md#remote-permission-approval).

The different UI variants shows basically the same information.
<table border="1" padding="3"><tr>
<td align="center"><img src="assets/screen-working.svg" width="160"></td>
<td align="center"><img src="assets/screen-waiting.svg" width="160"></td>
<td align="center"><img src="assets/screen-idle.svg" width="160"></td>
<tr><td>Claude Code working</td><td>Waiting for user input</td><td>Ready for a new task</td> 
</tr></table>


## Architecture

```mermaid
flowchart LR
    CC["Claude Code<br/>(CLI + VSCode plugin)"] -->|hooks fire on<br/>tool use / messages| D

    subgraph D["codelight.py daemon"]
        SOCK["Unix socket thread<br/>receives hook events"]
        USAGE["Usage poller<br/>claude.ai API every 60s"]
        WS["WebSocket server :8765"]
        DBUS["D-Bus service<br/>se.sensnology.codelight"]
    end

    WS -->|status broadcast| SCREEN["GeekMagic Ultra<br/>(mDNS)"]
    WS -->|status broadcast| ANDROID["Android widget<br/>(mDNS)"]
    WS -->|status| VSCODE["VSCode extension"]
    DBUS -->|status + permissions| GNOME["GNOME extension"]

    USAGE -.-> WS
    SOCK -.-> WS
    SOCK -.-> DBUS
```

The ESP8266 screen and Android widget use WebSocket (discovered via mDNS). The
GNOME extension uses D-Bus on the session bus — no network socket or
configuration needed. With `--remote-permissions`, permission prompts are also
pushed to the GNOME extension and the Android app for remote approval (see
[companion/README.md](companion/README.md#remote-permission-approval)).

## Quick start

1. Flash the screen firmware (or grab a pre-built `.bin` from the
   [Releases page](https://github.com/henrikekblad/codelight/releases)):
   see [screen/README.md](screen/README.md).

2. Run the companion daemon on your computer:
   ```bash
   python3 companion/codelight.py --name my-laptop
   ```
   Full setup: [companion/README.md](companion/README.md).

3. *(Optional)* Install the Android widget: [android/README.md](android/README.md).

4. *(Optional)* Install the GNOME extension: [gnome-extension/README.md](gnome-extension/README.md).
