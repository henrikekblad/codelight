package se.sensnology.codelight

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiInfo
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.pm.ServiceInfo
import android.os.Build
import android.util.Log
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.appwidget.updateAll
import androidx.glance.state.PreferencesGlanceStateDefinition
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

class CodelightService : LifecycleService() {

    companion object {
        const val STATE_PREFS    = "codelight_state"
        const val SETTINGS_PREFS = "codelight_settings"
        const val KEY_SESSION_PCT    = "session_pct"
        const val KEY_WEEKLY_PCT     = "weekly_pct"
        const val KEY_SESSION_RESET  = "session_reset"
        const val KEY_WEEKLY_RESET   = "weekly_reset"
        const val KEY_SESSION_RESET_AT = "session_reset_at"   // epoch seconds
        const val KEY_WEEKLY_RESET_AT  = "weekly_reset_at"
        const val KEY_STATUS          = "status"
        const val KEY_CONNECTED       = "connected"
        const val KEY_CONNECTED_HOST  = "connected_host"
        const val KEY_CONNECTED_PORT  = "connected_port"
        const val KEY_SECRET         = "secret"
        const val KEY_HOST           = "host"
        const val KEY_PORT           = "port"
        const val KEY_SELECTED_NAME    = "selected_name"
        const val KEY_DISCOVERED       = "discovered"   // JSON array of {name,host,port}
        const val KEY_NOTIFY_ON_IDLE    = "notify_idle"
        const val KEY_NOTIFY_ON_WAITING = "notify_waiting"
        const val KEY_NOTIFY_DELAY_SECS = "notify_delay"
        const val KEY_ALLOWED_SSIDS     = "allowed_ssids"
        const val KEY_PERMISSION_PROMPTS = "permission_prompts"

        private const val ALERT_NOTIF_ID    = 2
        private const val ALERT_CHANNEL_ID  = "codelight_alerts"
        private const val SVC_NOTIF_ID      = 1
        private const val SVC_CHANNEL_ID    = "codelight_service"
        private const val PAUSED_CHANNEL_ID = "codelight_paused"
        private const val PERM_CHANNEL_ID   = "codelight_permissions"
        private const val SERVICE_TYPE      = "_codelight._tcp"

        private const val ACTION_PERMISSION_RESPONSE = "se.sensnology.codelight.PERMISSION_RESPONSE"
        private const val EXTRA_REQUEST_ID = "request_id"
        private const val EXTRA_DECISION   = "decision"
    }

    data class DiscoveredService(val name: String, val host: String, val port: Int)

    private lateinit var nsdManager: NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var wifiCallback: ConnectivityManager.NetworkCallback? = null

    // Wi-Fi SSID filter: paused while not on an allowed network (see evaluateDormancy)
    private var currentSsid: String? = null
    private var dormant = false

    // Resolve one at a time via a queue (old NSD API limitation)
    private val resolveQueue = ArrayBlockingQueue<NsdServiceInfo>(32)
    private val resolving    = AtomicBoolean(false)

    private val httpClient = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(10, TimeUnit.SECONDS)
        .build()
    private var webSocket: WebSocket? = null

    private var reconnectJob: kotlinx.coroutines.Job? = null
    private var notifJob:     kotlinx.coroutines.Job? = null
    private var tickJob:      kotlinx.coroutines.Job? = null
    private var lastStatus = ""
    private var connectedName: String? = null   // mDNS name of the connected companion

    // Pending permission requests: request id → notification id
    private val permNotifIds = mutableMapOf<String, Int>()
    private var nextPermNotifId = 1000

    override fun onCreate() {
        super.onCreate()
        getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putBoolean(KEY_CONNECTED, false)
            .remove(KEY_CONNECTED_HOST)
            .remove(KEY_CONNECTED_PORT)
            .apply()
        createNotificationChannels()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(SVC_NOTIF_ID, buildServiceNotification("Connecting…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(SVC_NOTIF_ID, buildServiceNotification("Connecting…"))
        }
        pushWidgetUpdate()
        nsdManager = getSystemService(NSD_SERVICE) as NsdManager
        registerNetworkCallback()
        startDiscovery()

        // Refresh the widget every minute so the reset countdown stays live and
        // the bars zero out once a usage window has passed — even with no
        // connection, since the widget extrapolates from the stored reset time.
        tickJob = lifecycleScope.launch {
            while (true) {
                kotlinx.coroutines.delay(60_000)
                pushWidgetUpdate()
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        if (intent?.action == ACTION_PERMISSION_RESPONSE) {
            val id       = intent.getStringExtra(EXTRA_REQUEST_ID)
            val decision = intent.getStringExtra(EXTRA_DECISION)
            if (id != null && decision != null) sendPermissionResponse(id, decision)
        }
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        unregisterNetworkCallback()
        stopDiscovery()
        reconnectJob?.cancel()
        notifJob?.cancel()
        tickJob?.cancel()
        webSocket?.close(1000, "Service stopped")
        // Do NOT shut down httpClient.dispatcher.executorService here — doing so after
        // webSocket.close() causes any in-flight OkHttp callback that tries to reconnect
        // to fail with RejectedExecutionException, breaking the reconnect loop permanently.
    }

    private fun startDiscovery() {
        val settings   = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
        val manualHost = settings.getString(KEY_HOST, null)
        val manualPort = settings.getInt(KEY_PORT, 0)

        if (!manualHost.isNullOrBlank() && manualPort > 0) {
            connectWebSocket(manualHost, manualPort, null)
            return
        }

        resolveQueue.clear()

        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(type: String, code: Int) { scheduleReconnect() }
            override fun onStopDiscoveryFailed(type: String, code: Int)  {}
            override fun onDiscoveryStarted(type: String)                {}
            override fun onDiscoveryStopped(type: String)                {}

            override fun onServiceFound(service: NsdServiceInfo) {
                if (!service.serviceType.startsWith(SERVICE_TYPE)) return
                resolveQueue.offer(service)
                drainResolveQueue()
            }

            override fun onServiceLost(service: NsdServiceInfo) {
                removeDiscovered(service.serviceName)
                if (webSocket != null) {
                    val selected = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
                        .getString(KEY_SELECTED_NAME, null)
                    if (selected == null || selected == service.serviceName) {
                        webSocket?.close(1001, "Service lost")
                    }
                }
            }
        }
        try {
            nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
        } catch (_: Exception) {
            scheduleReconnect()
        }
    }

    private fun drainResolveQueue() {
        if (!resolving.compareAndSet(false, true)) return
        val service = resolveQueue.poll() ?: run { resolving.set(false); return }

        @Suppress("DEPRECATION")
        nsdManager.resolveService(service, object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, code: Int) {
                resolving.set(false)
                drainResolveQueue()
            }
            override fun onServiceResolved(info: NsdServiceInfo) {
                resolving.set(false)
                @Suppress("DEPRECATION")
                val host = info.host?.hostAddress ?: run { drainResolveQueue(); return }
                val port = info.port
                val name = info.serviceName
                addDiscovered(DiscoveredService(name, host, port))
                drainResolveQueue()
                maybeConnect(name, host, port)
            }
        })
    }

    private fun maybeConnect(name: String, host: String, port: Int) {
        if (webSocket != null) return  // already connected
        val selected = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
            .getString(KEY_SELECTED_NAME, null)
        if (selected == null || selected == name) {
            connectWebSocket(host, port, name)
        }
    }

    private fun addDiscovered(svc: DiscoveredService) {
        val prefs = getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
        val list  = loadDiscovered(prefs).toMutableList()
        list.removeAll { it.name == svc.name }
        list.add(svc)
        saveDiscovered(list)
    }

    private fun removeDiscovered(name: String) {
        val prefs = getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
        val list  = loadDiscovered(prefs).filter { it.name != name }
        saveDiscovered(list)
    }

    private fun saveDiscovered(list: List<DiscoveredService>) {
        val arr = JSONArray()
        list.forEach { s ->
            arr.put(JSONObject().put("name", s.name).put("host", s.host).put("port", s.port))
        }
        getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putString(KEY_DISCOVERED, arr.toString()).apply()
    }

    private fun stopDiscovery() {
        try { discoveryListener?.let { nsdManager.stopServiceDiscovery(it) } } catch (_: Exception) {}
        discoveryListener = null
    }

    private fun connectWebSocket(host: String, port: Int, name: String?) {
        Log.d("Codelight", "Connecting to ws://$host:$port ($name)")
        connectedName = name
        val secret  = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE).getString(KEY_SECRET, "") ?: ""
        val request = Request.Builder().url("ws://$host:$port").build()

        webSocket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                // With a secret the daemon sends a challenge first and we reply
                // with an HMAC (see parseAndStore); without one, subscribe now.
                if (secret.isBlank()) subscribe(webSocket)
                Log.i("Codelight", "WS onOpen host=$host port=$port auth=${secret.isNotBlank()}")
                reconnectJob?.cancel()
                reconnectJob = null
                setConnected(true, host, port)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                Log.i("Codelight", "WS onMessage: $text")
                parseAndStore(text)
                pushWidgetUpdate()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.w("Codelight", "WS onClosed code=$code reason=$reason")
                this@CodelightService.webSocket = null
                setConnected(false)
                if (code == 1008) {
                    sendAuthFailedNotification()
                } else {
                    scheduleReconnect()
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e("Codelight", "WS onFailure: ${t.javaClass.simpleName}: ${t.message}")
                this@CodelightService.webSocket = null
                setConnected(false)
                scheduleReconnect()
            }
        })
    }

    private fun subscribe(ws: WebSocket) {
        if (getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
                .getBoolean(KEY_PERMISSION_PROMPTS, true)) {
            ws.send("""{"type":"subscribe","features":["permissions"],"client":"android"}""")
        }
    }

    /** Prove knowledge of the secret without sending it: HMAC-SHA256(secret, nonce). */
    private fun respondChallenge(nonce: String) {
        val secret = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE).getString(KEY_SECRET, "") ?: ""
        val ws = webSocket ?: return
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(), "HmacSHA256"))
        val proof = mac.doFinal(nonce.toByteArray()).joinToString("") { "%02x".format(it) }
        ws.send("""{"auth_hmac":"$proof"}""")
        subscribe(ws)
    }

    private fun parseAndStore(json: String) {
        try {
            val obj  = JSONObject(json)
            when (obj.optString("type")) {
                "config"              -> return
                "challenge"           -> { respondChallenge(obj.optString("nonce")); return }
                "permission_request"  -> { showPermissionNotification(obj); return }
                "permission_resolved" -> { cancelPermissionNotification(obj.optString("id")); return }
            }
            val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            if (obj.has("session_pct"))   edit.putFloat(KEY_SESSION_PCT,   obj.getDouble("session_pct").toFloat())
            if (obj.has("weekly_pct"))    edit.putFloat(KEY_WEEKLY_PCT,    obj.getDouble("weekly_pct").toFloat())
            if (obj.has("session_reset")) edit.putString(KEY_SESSION_RESET, obj.getString("session_reset"))
            if (obj.has("weekly_reset"))  edit.putString(KEY_WEEKLY_RESET,  obj.getString("weekly_reset"))
            if (obj.has("session_reset_at")) edit.putLong(KEY_SESSION_RESET_AT, obj.getLong("session_reset_at"))
            if (obj.has("weekly_reset_at"))  edit.putLong(KEY_WEEKLY_RESET_AT,  obj.getLong("weekly_reset_at"))
            if (obj.has("status")) {
                // companions < 1.0.9 send "inactive" for what is now "idle"
                val newStatus = obj.getString("status").let { if (it == "inactive") "idle" else it }
                edit.putString(KEY_STATUS, newStatus)
                if (newStatus != lastStatus) {
                    Log.i("Codelight", "status changed: $lastStatus → $newStatus")
                    lastStatus = newStatus
                    maybeScheduleNotification(newStatus)
                }
            }
            edit.apply()
            Log.d("Codelight", "parseAndStore: status=${obj.optString("status","?")} sessions=${obj.optInt("sessions",-1)}")
        } catch (e: Exception) {
            Log.e("Codelight", "parseAndStore error: $e")
        }
    }

    private fun maybeScheduleNotification(status: String) {
        notifJob?.cancel()
        // A visible alert is stale once the status changes (e.g. the permission
        // request was already answered at the computer)
        getSystemService(NotificationManager::class.java).cancel(ALERT_NOTIF_ID)
        val prefs         = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
        val notifyIdle    = prefs.getBoolean(KEY_NOTIFY_ON_IDLE,    false)
        val notifyWaiting = prefs.getBoolean(KEY_NOTIFY_ON_WAITING, false)
        val delaySecs     = prefs.getInt(KEY_NOTIFY_DELAY_SECS, 30).toLong()

        // A pending permission request already shows its own notification —
        // don't also raise the generic "waiting for input" alert.
        val shouldNotify = when (status) {
            "idle" -> notifyIdle
            "waiting"  -> notifyWaiting && permNotifIds.isEmpty()
            else       -> false
        }
        if (!shouldNotify) return

        notifJob = lifecycleScope.launch {
            kotlinx.coroutines.delay(delaySecs * 1_000L)
            sendAlertNotification(status)
        }
    }

    private fun sendAlertNotification(status: String) {
        val text = when (status) {
            "waiting" -> "Claude is waiting for your input"
            "idle"    -> "Claude is idle — session ended"
            else      -> return
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, SettingsActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(this, ALERT_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle("codelight")
            .setContentText(text)
            .setContentIntent(pi)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(ALERT_NOTIF_ID, notif)
    }

    // ── Remote permission approval ────────────────────────────────────────────

    private fun showPermissionNotification(obj: JSONObject) {
        val id = obj.optString("id")
        if (id.isEmpty() || permNotifIds.containsKey(id)) return   // duplicate/replay
        if (!getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
                .getBoolean(KEY_PERMISSION_PROMPTS, true)) return

        // The "waiting" status broadcast arrives just before this and may have
        // scheduled/shown the generic waiting alert — supersede it.
        notifJob?.cancel()
        getSystemService(NotificationManager::class.java).cancel(ALERT_NOTIF_ID)

        val notifId = nextPermNotifId++
        permNotifIds[id] = notifId

        fun actionIntent(decision: String, requestCode: Int): PendingIntent {
            val intent = Intent(this, CodelightService::class.java)
                .setAction(ACTION_PERMISSION_RESPONSE)
                .putExtra(EXTRA_REQUEST_ID, id)
                .putExtra(EXTRA_DECISION, decision)
            return PendingIntent.getService(
                this, requestCode, intent,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
        }

        val summary   = obj.optString("summary", obj.optString("tool_name", "tool use"))
        val expiresAt = obj.optLong("expires_at", 0)
        val builder = NotificationCompat.Builder(this, PERM_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_warning)
            .setContentTitle("Claude Code asks")
            .setContentText(summary)
            .setStyle(NotificationCompat.BigTextStyle().bigText(summary))
            .addAction(0, "Allow", actionIntent("allow", notifId * 2))
            .addAction(0, "Deny",  actionIntent("deny",  notifId * 2 + 1))
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setOngoing(true)
        if (expiresAt > 0) {
            val ttl = expiresAt * 1000 - System.currentTimeMillis()
            if (ttl > 0) builder.setTimeoutAfter(ttl)   // auto-dismiss at daemon timeout
        }
        getSystemService(NotificationManager::class.java).notify(notifId, builder.build())
    }

    private fun cancelPermissionNotification(id: String) {
        permNotifIds.remove(id)?.let {
            getSystemService(NotificationManager::class.java).cancel(it)
        }
    }

    private fun sendPermissionResponse(id: String, decision: String) {
        val sent = webSocket?.send(
            """{"type":"permission_response","id":"$id","decision":"$decision"}"""
        ) ?: false
        Log.i("Codelight", "permission $decision for $id (sent=$sent)")
        cancelPermissionNotification(id)
    }

    private fun sendAuthFailedNotification() {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, SettingsActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val notif = NotificationCompat.Builder(this, ALERT_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_error)
            .setContentTitle("codelight")
            .setContentText("Wrong password — tap to fix")
            .setContentIntent(pi)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(ALERT_NOTIF_ID, notif)
    }

    private fun setConnected(connected: Boolean, host: String = "", port: Int = 0) {
        Log.i("Codelight", "setConnected: connected=$connected host=$host port=$port dormant=$dormant")
        val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putBoolean(KEY_CONNECTED, connected)
        if (connected) {
            edit.putString(KEY_CONNECTED_HOST, host).putInt(KEY_CONNECTED_PORT, port)
        } else {
            edit.remove(KEY_CONNECTED_HOST).remove(KEY_CONNECTED_PORT)
        }
        edit.apply()
        if (!connected) {
            connectedName = null
            // A posted alert can't be trusted once we lose sight of the companion
            notifJob?.cancel()
            getSystemService(NotificationManager::class.java).cancel(ALERT_NOTIF_ID)
        }
        val selected = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
            .getString(KEY_SELECTED_NAME, null)
        val nm = getSystemService(NotificationManager::class.java)
        when {
            connected -> nm.notify(SVC_NOTIF_ID,
                buildServiceNotification("Connected to ${connectedName ?: selected ?: "codelight"}"))
            dormant   -> nm.notify(SVC_NOTIF_ID,
                buildServiceNotification("Paused — waiting for home Wi-Fi", PAUSED_CHANNEL_ID))
            else      -> nm.notify(SVC_NOTIF_ID,
                buildServiceNotification("Searching for ${selected ?: "codelight"}…"))
        }
        pushWidgetUpdate()
    }

    private fun pushWidgetUpdate() {
        lifecycleScope.launch {
            try {
                val manager  = GlanceAppWidgetManager(this@CodelightService)
                val glanceIds = manager.getGlanceIds(CodelightWidget::class.java)
                if (glanceIds.isEmpty()) return@launch
                glanceIds.forEach { id ->
                    updateAppWidgetState(
                        this@CodelightService, PreferencesGlanceStateDefinition, id
                    ) { prefs ->
                        prefs.toMutablePreferences().apply {
                            this[CodelightWidget.KEY_TICK] =
                                (this[CodelightWidget.KEY_TICK] ?: 0) + 1
                        }
                    }
                }
                CodelightWidget().updateAll(this@CodelightService)
                Log.d("Codelight", "pushWidgetUpdate done")
            } catch (e: Exception) {
                Log.e("Codelight", "pushWidgetUpdate error: $e")
            }
        }
    }

    private fun isSsidAllowed(ssid: String): Boolean {
        val allowed = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
            .getStringSet(KEY_ALLOWED_SSIDS, emptySet()) ?: emptySet()
        return allowed.isEmpty() || ssid in allowed
    }

    private fun registerNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager

        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // Only restart if we have no live connection — otherwise a secondary
                // network becoming available (mobile data, VPN, etc.) would tear down
                // a perfectly good WiFi WebSocket every 5 seconds.
                if (webSocket == null && !dormant) {
                    Log.d("Codelight", "Default network available, restarting discovery")
                    scheduleReconnect()
                }
            }

            override fun onLost(network: Network) {
                // Primary network lost — drop socket immediately so the reconnect
                // timer starts now rather than waiting for the ping timeout.
                Log.d("Codelight", "Default network lost, dropping connection")
                webSocket?.cancel()
            }
        }
        cm.registerDefaultNetworkCallback(cb)
        networkCallback = cb

        // Track the phone's WiFi network (not the default network: with a VPN up
        // the default is the tunnel and its capabilities hide the SSID) so the
        // SSID filter can pause the service whenever we're not on an allowed
        // network — foreign WiFi, cellular, or VPN-only.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val req = NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .build()
            val wcb = object : ConnectivityManager.NetworkCallback(FLAG_INCLUDE_LOCATION_INFO) {
                override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
                    currentSsid = (capabilities.transportInfo as? WifiInfo)
                        ?.ssid?.removeSurrounding("\"")
                    evaluateDormancy()
                }

                override fun onLost(network: Network) {
                    currentSsid = null
                    evaluateDormancy()
                }
            }
            cm.registerNetworkCallback(req, wcb)
            wifiCallback = wcb
        }
    }

    private fun unregisterNetworkCallback() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        networkCallback?.let { try { cm.unregisterNetworkCallback(it) } catch (_: Exception) {} }
        networkCallback = null
        wifiCallback?.let { try { cm.unregisterNetworkCallback(it) } catch (_: Exception) {} }
        wifiCallback = null
    }

    private fun evaluateDormancy() {
        val allowed = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
            .getStringSet(KEY_ALLOWED_SSIDS, emptySet()) ?: emptySet()
        if (allowed.isEmpty()) { exitDormant(); return }   // filter disabled

        val ssid = currentSsid
        // Fail open when on WiFi but the SSID is unreadable (missing permission)
        val active = ssid != null && (ssid == "<unknown ssid>" || ssid in allowed)
        if (active) exitDormant() else enterDormant()
    }

    private fun enterDormant() {
        if (dormant) return
        dormant = true
        Log.i("Codelight", "Not on an allowed WiFi (ssid=$currentSsid) — pausing")
        reconnectJob?.cancel()
        reconnectJob = null
        stopDiscovery()
        webSocket?.cancel()
        webSocket = null
        setConnected(false)
    }

    private fun exitDormant() {
        if (!dormant) return
        dormant = false
        Log.i("Codelight", "Allowed WiFi '$currentSsid' available — resuming")
        setConnected(false)   // restores the normal "Searching…" notification
        scheduleReconnect()
    }

    private fun scheduleReconnect() {
        if (dormant) return
        Log.i("Codelight", "scheduleReconnect (reconnecting in 5 s)")
        reconnectJob?.cancel()
        reconnectJob = lifecycleScope.launch {
            kotlinx.coroutines.delay(5_000)
            stopDiscovery()
            webSocket?.cancel()
            webSocket = null
            startDiscovery()
        }
    }

    private fun buildServiceNotification(text: String, channel: String = SVC_CHANNEL_ID) =
        NotificationCompat.Builder(this, channel)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("codelight")
            .setContentText(text)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(
                PendingIntent.getActivity(
                    this, 0, Intent(this, SettingsActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE,
                )
            )
            .build()

    private fun createNotificationChannels() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(SVC_CHANNEL_ID, "codelight service", NotificationManager.IMPORTANCE_LOW)
                .apply { setShowBadge(false) }
        )
        nm.createNotificationChannel(
            NotificationChannel(ALERT_CHANNEL_ID, "codelight alerts", NotificationManager.IMPORTANCE_HIGH)
                .apply { enableVibration(true); enableLights(true) }
        )
        // MIN importance: no status-bar icon, collapsed at the bottom of the shade
        nm.createNotificationChannel(
            NotificationChannel(PAUSED_CHANNEL_ID, "codelight paused", NotificationManager.IMPORTANCE_MIN)
                .apply { setShowBadge(false) }
        )
        nm.createNotificationChannel(
            NotificationChannel(PERM_CHANNEL_ID, "codelight permission requests",
                NotificationManager.IMPORTANCE_HIGH)
                .apply { enableVibration(true); enableLights(true) }
        )
    }
}

fun loadDiscovered(prefs: android.content.SharedPreferences): List<CodelightService.DiscoveredService> {
    val json = prefs.getString(CodelightService.KEY_DISCOVERED, null) ?: return emptyList()
    return try {
        val arr = JSONArray(json)
        (0 until arr.length()).map {
            val o = arr.getJSONObject(it)
            CodelightService.DiscoveredService(o.getString("name"), o.getString("host"), o.getInt("port"))
        }
    } catch (_: Exception) { emptyList() }
}
