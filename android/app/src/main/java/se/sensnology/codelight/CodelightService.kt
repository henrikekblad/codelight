package se.sensnology.codelight

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
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
        const val KEY_AGENT_ID        = "agent_id"
        const val KEY_AGENT_DISPLAY   = "agent_display"
        const val KEY_WEEKLY_TITLE    = "weekly_title"
        const val KEY_SESSION_TITLE   = "session_title"
        const val KEY_PER_AGENT_USAGE = "per_agent_usage"
        const val KEY_PER_AGENT_STATUS = "per_agent_status"
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
        const val KEY_QUESTION_PROMPTS   = "question_prompts"
        const val KEY_AUTO_OPEN          = "auto_open"          // full-screen intent
        const val KEY_PENDING_REQUESTS   = "pending_requests"   // JSON {id → request}
        const val KEY_CONV_LINES         = "conv_lines"         // how many lines to show
        const val KEY_REMOTE_CONTROL     = "remote_control"     // companion arms tabs
        const val KEY_CONVERSATION       = "conversation"       // JSON [{role,text}]

        private const val ALERT_NOTIF_ID    = 2
        private const val ALERT_CHANNEL_ID  = "codelight_alerts"
        private const val SVC_NOTIF_ID      = 1
        private const val SVC_CHANNEL_ID    = "codelight_service"
        private const val PAUSED_CHANNEL_ID = "codelight_paused"
        private const val PERM_CHANNEL_ID   = "codelight_permissions"
        private const val SERVICE_TYPE      = "_codelight._tcp"

        const val ACTION_PERMISSION_RESPONSE = "se.sensnology.codelight.PERMISSION_RESPONSE"
        const val ACTION_QUESTION_RESPONSE   = "se.sensnology.codelight.QUESTION_RESPONSE"
        const val ACTION_EXTEND              = "se.sensnology.codelight.EXTEND"
        const val EXTRA_REQUEST_ID = "request_id"
        const val EXTRA_DECISION   = "decision"
        const val EXTRA_ANSWERS    = "answers"   // JSON {question → answer}
        const val EXTRA_TAB        = "tab"       // MainActivity initial tab
    }

    data class DiscoveredService(val name: String, val host: String, val port: Int)

    private lateinit var nsdManager: NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var wifiCallback: ConnectivityManager.NetworkCallback? = null
    private var defaultNetwork: Network? = null   // current default network, to detect changes

    // Wi-Fi SSID filter: paused while not on an allowed network (see evaluateDormancy)
    private var currentSsid: String? = null
    private var wifiAvailable = false   // is a Wi-Fi network currently up (SSID may be unknown)
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
        val id = intent?.getStringExtra(EXTRA_REQUEST_ID)
        when (intent?.action) {
            ACTION_PERMISSION_RESPONSE -> {
                val decision = intent.getStringExtra(EXTRA_DECISION)
                if (id != null && decision != null) sendPermissionResponse(id, decision)
            }
            ACTION_QUESTION_RESPONSE -> {
                val answers = intent.getStringExtra(EXTRA_ANSWERS)   // JSON or null=skip
                if (id != null) sendQuestionResponse(id, answers)
            }
            ACTION_EXTEND -> {
                if (id != null) webSocket?.send("""{"type":"extend","id":"$id"}""")
            }
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
        val prefs = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
        val features = mutableListOf<String>()
        if (prefs.getBoolean(KEY_PERMISSION_PROMPTS, true)) features.add("\"permissions\"")
        if (prefs.getBoolean(KEY_QUESTION_PROMPTS, true))   features.add("\"questions\"")
        // Always request the conversation feed; the daemon only serves it when
        // it runs with --remote-control, so this is a no-op otherwise.
        features.add("\"conversation\"")
        ws.send("""{"type":"subscribe","features":[${features.joinToString(",")}],"client":"android"}""")
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
                "config"              -> {
                    getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
                        .putBoolean(KEY_REMOTE_CONTROL, obj.optBoolean("remote_control", false))
                        .apply()
                    return
                }
                "challenge"           -> { respondChallenge(obj.optString("nonce")); return }
                "permission_request"  -> { onRequest(obj, "permission"); return }
                "question_request"    -> { onRequest(obj, "question"); return }
                "permission_resolved",
                "question_resolved"   -> { resolveRequest(obj.optString("id")); return }
                "conversation"        -> { storeConversation(obj); return }
            }
            val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            if (obj.has("agent_id"))      edit.putString(KEY_AGENT_ID, obj.optString("agent_id", "claude"))
            if (obj.has("agent_display")) edit.putString(KEY_AGENT_DISPLAY, obj.optString("agent_display", "Claude"))
            if (obj.has("weekly_title"))  edit.putString(KEY_WEEKLY_TITLE, obj.optString("weekly_title", "Claude Weekly"))
            if (obj.has("session_title")) edit.putString(KEY_SESSION_TITLE, obj.optString("session_title", "Claude Session"))
            if (obj.has("per_agent_usage")) edit.putString(KEY_PER_AGENT_USAGE, obj.getJSONObject("per_agent_usage").toString())
            if (obj.has("per_agent_status")) edit.putString(KEY_PER_AGENT_STATUS, obj.getJSONObject("per_agent_status").toString())
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

        // A pending request already has its own screen/notification (or auto-
        // opened) — don't also raise the generic "waiting for input" alert.
        val shouldNotify = when (status) {
            "idle" -> notifyIdle
            "waiting"  -> notifyWaiting && permNotifIds.isEmpty() && !hasPendingRequests()
            else       -> false
        }
        if (!shouldNotify) return

        notifJob = lifecycleScope.launch {
            kotlinx.coroutines.delay(delaySecs * 1_000L)
            sendAlertNotification(status)
        }
    }

    private fun sendAlertNotification(status: String) {
        val agent = currentAgentDisplayName()
        val text = when (status) {
            "waiting" -> "$agent is waiting for your input"
            "idle"    -> "$agent is idle — session ended"
            else      -> return
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
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

    // ── Remote requests (permission + question) ───────────────────────────────
    // The notification is just a tap-target; the full request + controls live in
    // RequestActivity so nothing is clipped and questions get rich controls.

    private fun onRequest(obj: JSONObject, kind: String) {
        val id = obj.optString("id")
        val agent = obj.optString("agent_display", currentAgentDisplayName())
        val prefs = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
        val enabled = if (kind == "question") prefs.getBoolean(KEY_QUESTION_PROMPTS, true)
                      else prefs.getBoolean(KEY_PERMISSION_PROMPTS, true)
        if (id.isEmpty() || permNotifIds.containsKey(id) || !enabled) return

        // A "waiting" status broadcast arrives just before this — supersede its alert.
        notifJob?.cancel()
        getSystemService(NotificationManager::class.java).cancel(ALERT_NOTIF_ID)

        obj.put("kind", kind)
        putPendingRequest(id, obj)

        val open = Intent(this, RequestActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(EXTRA_REQUEST_ID, id)

        // Auto-open: with the overlay permission we're exempt from background
        // activity-launch limits, so pop the screen directly (works unlocked
        // too). When it succeeds, skip the heads-up notification — it would just
        // land on top of the request screen we just opened. Fall back to the
        // notification only if the launch throws.
        val autoOpen = prefs.getBoolean(KEY_AUTO_OPEN, false)
        if (autoOpen && android.provider.Settings.canDrawOverlays(this)) {
            try { startActivity(open); return } catch (_: Exception) {}
        }

        val notifId = nextPermNotifId++
        permNotifIds[id] = notifId
        val pi = PendingIntent.getActivity(this, notifId, open,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

        val summary = if (kind == "question")
            obj.optJSONArray("questions")?.optJSONObject(0)?.optString("question") ?: "$agent has a question"
        else obj.optString("summary", obj.optString("tool_name", "tool use"))
        val builder = NotificationCompat.Builder(this, PERM_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_warning)
            .setContentTitle(if (kind == "question") "$agent asks a question" else "$agent asks")
            .setContentText(summary)
            .setStyle(NotificationCompat.BigTextStyle().bigText(summary))
            .setContentIntent(pi)
            .setAutoCancel(true)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setOngoing(true)
        val expiresAt = obj.optLong("expires_at", 0)
        if (expiresAt > 0) {
            val ttl = expiresAt * 1000 - System.currentTimeMillis()
            if (ttl > 0) builder.setTimeoutAfter(ttl)
        }
        getSystemService(NotificationManager::class.java).notify(notifId, builder.build())
    }

    private fun storeConversation(obj: JSONObject) {
        // Mirror the companion's conversation feed to STATE_PREFS for the tab.
        val lines = obj.optJSONArray("lines") ?: JSONArray()
        val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putString(KEY_CONVERSATION, lines.toString())
        if (obj.has("agent_id")) {
            edit.putString(KEY_AGENT_ID, obj.optString("agent_id", "claude"))
        }
        if (obj.has("agent_display")) {
            edit.putString(KEY_AGENT_DISPLAY, obj.optString("agent_display", "Claude"))
        }
        edit.apply()
    }

    private fun resolveRequest(id: String) {
        removePendingRequest(id)
        permNotifIds.remove(id)?.let {
            getSystemService(NotificationManager::class.java).cancel(it)
        }
    }

    private fun sendPermissionResponse(id: String, decision: String) {
        webSocket?.send("""{"type":"permission_response","id":"$id","decision":"$decision"}""")
        resolveRequest(id)
    }

    private fun sendQuestionResponse(id: String, answersJson: String?) {
        // answersJson null/blank → skip (daemon replies null → local fall-through)
        val answers = answersJson?.takeIf { it.isNotBlank() } ?: "{}"
        webSocket?.send("""{"type":"question_response","id":"$id","answers":$answers}""")
        resolveRequest(id)
    }

    // Pending requests are mirrored to STATE_PREFS so RequestActivity can render them.
    private fun putPendingRequest(id: String, obj: JSONObject) {
        val prefs = getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
        val all = try { JSONObject(prefs.getString(KEY_PENDING_REQUESTS, "{}") ?: "{}") } catch (_: Exception) { JSONObject() }
        all.put(id, obj)
        prefs.edit().putString(KEY_PENDING_REQUESTS, all.toString()).apply()
    }

    private fun hasPendingRequests(): Boolean {
        val prefs = getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
        return try {
            JSONObject(prefs.getString(KEY_PENDING_REQUESTS, "{}") ?: "{}").length() > 0
        } catch (_: Exception) { false }
    }

    private fun removePendingRequest(id: String) {
        val prefs = getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
        val all = try { JSONObject(prefs.getString(KEY_PENDING_REQUESTS, "{}") ?: "{}") } catch (_: Exception) { JSONObject() }
        all.remove(id)
        prefs.edit().putString(KEY_PENDING_REQUESTS, all.toString()).apply()
    }

    private fun clearAllPending() {
        getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putString(KEY_PENDING_REQUESTS, "{}").apply()
        val nm = getSystemService(NotificationManager::class.java)
        permNotifIds.values.forEach { nm.cancel(it) }
        permNotifIds.clear()
    }

    private fun sendAuthFailedNotification() {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
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
            // A posted alert/request can't be trusted once we lose the companion;
            // pending requests are replayed by the daemon on reconnect.
            notifJob?.cancel()
            getSystemService(NotificationManager::class.java).cancel(ALERT_NOTIF_ID)
            clearAllPending()
        }
        val selected = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
            .getString(KEY_SELECTED_NAME, null)
        val nm = getSystemService(NotificationManager::class.java)
        when {
            connected -> nm.notify(SVC_NOTIF_ID,
                buildServiceNotification("Connected to ${connectedName ?: selected ?: "codelight"}"))
            // While paused off the home network the notification is hidden
            // entirely (stopForeground in enterDormant) — don't re-post it here.
            dormant   -> { /* intentionally no notification while paused */ }
            else      -> nm.notify(SVC_NOTIF_ID,
                buildServiceNotification("Searching for ${selected ?: "codelight"}…"))
        }
        pushWidgetUpdate()
    }

    private fun currentAgentDisplayName(): String {
        return getSharedPreferences(STATE_PREFS, MODE_PRIVATE)
            .getString(KEY_AGENT_DISPLAY, "Claude") ?: "Claude"
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
                // A *changed* default network (Wi-Fi handoff home→work, docking, VPN)
                // means any existing socket is bound to the old, now-dead network —
                // OkHttp's ping-based failure detection is frozen in Doze so the
                // socket never nulls itself, and a `webSocket == null` guard would
                // skip the reconnect. So on a real change, drop the stale socket and
                // reconnect. Network callbacks fire even in Doze, so this recovers
                // without the user opening the app. (Same default network re-firing
                // is ignored, so a stable connection never churns.)
                val changed = defaultNetwork != null && network != defaultNetwork
                defaultNetwork = network
                if (dormant) {
                    // If Wi-Fi just came back, re-check dormancy before early-returning.
                    val caps = cm.getNetworkCapabilities(network)
                    if (caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) {
                        wifiAvailable = true
                    }
                    evaluateDormancy()
                    if (dormant) return
                }
                if (changed) {
                    Log.d("Codelight", "Default network changed — forcing reconnect")
                    webSocket?.cancel()
                    webSocket = null
                    scheduleReconnect()
                } else if (webSocket == null) {
                    Log.d("Codelight", "Default network available, restarting discovery")
                    scheduleReconnect()
                }
            }

            override fun onLost(network: Network) {
                // Primary network lost — drop the socket AND null it immediately (don't
                // wait for OkHttp's onFailure, which is deferred in Doze) so the next
                // onAvailable isn't blocked by a stale reference, and update the UI now.
                Log.d("Codelight", "Default network lost, dropping connection")
                if (network == defaultNetwork) defaultNetwork = null
                webSocket?.cancel()
                webSocket = null
                setConnected(false)
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
                override fun onAvailable(network: Network) {
                    wifiAvailable = true
                    Log.d("Codelight", "WiFi onAvailable: network=$network")
                    evaluateDormancy()
                }

                override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
                    wifiAvailable = true
                    currentSsid = (capabilities.transportInfo as? WifiInfo)
                        ?.ssid?.removeSurrounding("\"")
                    Log.d("Codelight", "WiFi onCapabilitiesChanged: ssid=$currentSsid")
                    evaluateDormancy()
                }

                override fun onLost(network: Network) {
                    wifiAvailable = false
                    currentSsid = null
                    Log.d("Codelight", "WiFi onLost: network=$network")
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

        if (!wifiAvailable) { enterDormant(); return }

        val ssid = currentSsid
        // Fail open when Wi-Fi is up but SSID is not yet available/readable.
        val active = ssid == null || ssid == "<unknown ssid>" || ssid in allowed
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
        setConnected(false)               // restores the normal "Searching…" text
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
                    this, 0, Intent(this, MainActivity::class.java),
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
