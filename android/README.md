# codelight — Android widget

A home-screen widget that shows weekly and session token usage bars plus the
current Claude Code status. Updates instantly via WebSocket whenever the
companion daemon sends new data.

<img src="../assets/android.jpg" width="360" alt="codelight Android widget">

## Setup

1. Install the optional Python dependencies on your computer (see
   [companion/README.md](../companion/README.md)):
   ```bash
   pip install websockets zeroconf
   ```

2. Start the companion daemon with `--secret` (recommended):
   ```bash
   python3 companion/codelight.py --secret mypassword
   ```

3. Build and install the Android app:
   - Open the `android/` directory as a project in Android Studio, or
   - Sideload the APK from the [Releases page](https://github.com/henrikekblad/codelight/releases).

4. Add the **codelight** widget to your home screen.

5. Open the **codelight** app (or tap the widget) and enter the password.
   Leave it blank if you did not set `--secret`.

The widget connects automatically and stays updated in the background.

## Hiding the persistent notification

The app runs as a foreground service, which Android requires to show a
persistent notification. To hide it from the status bar without stopping
the service:

1. Long-press the notification in the drawer and tap the cog icon.
2. Tap **Notification categories** → **codelight service**.
3. Select **Minimize notification**.

The service keeps running; only the status bar icon disappears.

## How it discovers the daemon

The daemon advertises itself on the local network via mDNS (`_codelight._tcp`).
The Android app uses `NsdManager` to find it automatically — no IP address
configuration required. On reconnect after a network change it rediscovers the
daemon via mDNS.

## Manual host override

If mDNS doesn't work on your network, open the codelight app, enter the
daemon's IP address and port (default 8765), and save. The app skips
discovery and connects directly.

## Samsung battery optimization

Samsung's "App sleeping" can kill the background service. To prevent this:

1. Long-press the app icon and tap the **i** (App Info) button.
2. Tap **Battery**.
3. Change from **Optimized** to **Unrestricted**.

## Firewall

The app connects to port 8765 (TCP) and uses mDNS on port 5353 (UDP) for
discovery. See [companion/README.md](../companion/README.md#firewall) for
firewall configuration.
