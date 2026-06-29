package se.sensnology.codelight

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
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

class CodelightService : LifecycleService() {

    companion object {
        const val STATE_PREFS    = "codelight_state"
        const val SETTINGS_PREFS = "codelight_settings"
        const val KEY_SESSION_PCT    = "session_pct"
        const val KEY_WEEKLY_PCT     = "weekly_pct"
        const val KEY_SESSION_RESET  = "session_reset"
        const val KEY_WEEKLY_RESET   = "weekly_reset"
        const val KEY_STATUS          = "status"
        const val KEY_CONNECTED       = "connected"
        const val KEY_CONNECTED_HOST  = "connected_host"
        const val KEY_CONNECTED_PORT  = "connected_port"
        const val KEY_SECRET         = "secret"
        const val KEY_HOST           = "host"
        const val KEY_PORT           = "port"
        const val KEY_SELECTED_NAME    = "selected_name"
        const val KEY_DISCOVERED       = "discovered"   // JSON array of {name,host,port}
        const val KEY_NOTIFY_ON_IDLE   = "notify_idle"
        const val KEY_NOTIFY_ON_WAITING = "notify_waiting"
        const val KEY_NOTIFY_DELAY_SECS = "notify_delay"

        private const val ALERT_NOTIF_ID    = 2
        private const val ALERT_CHANNEL_ID  = "codelight_alerts"
        private const val SVC_NOTIF_ID      = 1
        private const val SVC_CHANNEL_ID    = "codelight_service"
        private const val SERVICE_TYPE      = "_codelight._tcp"
    }

    data class DiscoveredService(val name: String, val host: String, val port: Int)

    private lateinit var nsdManager: NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

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
    private var lastStatus = ""

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
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        return START_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        unregisterNetworkCallback()
        stopDiscovery()
        reconnectJob?.cancel()
        notifJob?.cancel()
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
            connectWebSocket(manualHost, manualPort)
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
            connectWebSocket(host, port)
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

    private fun connectWebSocket(host: String, port: Int) {
        Log.d("Codelight", "Connecting to ws://$host:$port")
        val secret  = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE).getString(KEY_SECRET, "") ?: ""
        val request = Request.Builder().url("ws://$host:$port").build()

        webSocket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                val authing = secret.isNotBlank()
                if (authing) webSocket.send("""{"auth":"$secret"}""")
                Log.i("Codelight", "WS onOpen host=$host port=$port auth=$authing")
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
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e("Codelight", "WS onFailure: ${t.javaClass.simpleName}: ${t.message}")
                this@CodelightService.webSocket = null
                setConnected(false)
                scheduleReconnect()
            }
        })
    }

    private fun parseAndStore(json: String) {
        try {
            val obj  = JSONObject(json)
            val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            if (obj.has("session_pct"))   edit.putFloat(KEY_SESSION_PCT,   obj.getDouble("session_pct").toFloat())
            if (obj.has("weekly_pct"))    edit.putFloat(KEY_WEEKLY_PCT,    obj.getDouble("weekly_pct").toFloat())
            if (obj.has("session_reset")) edit.putString(KEY_SESSION_RESET, obj.getString("session_reset"))
            if (obj.has("weekly_reset"))  edit.putString(KEY_WEEKLY_RESET,  obj.getString("weekly_reset"))
            if (obj.has("status")) {
                val newStatus = obj.getString("status")
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
        val prefs         = getSharedPreferences(SETTINGS_PREFS, MODE_PRIVATE)
        val notifyIdle    = prefs.getBoolean(KEY_NOTIFY_ON_IDLE,    false)
        val notifyWaiting = prefs.getBoolean(KEY_NOTIFY_ON_WAITING, false)
        val delaySecs     = prefs.getInt(KEY_NOTIFY_DELAY_SECS, 30).toLong()

        val shouldNotify = when (status) {
            "inactive" -> notifyIdle
            "waiting"  -> notifyWaiting
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
            "waiting"  -> "Waiting for your input"
            "inactive" -> "Session ended (IDLE)"
            else       -> return
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

    private fun setConnected(connected: Boolean, host: String = "", port: Int = 0) {
        Log.i("Codelight", "setConnected: connected=$connected host=$host port=$port")
        val edit = getSharedPreferences(STATE_PREFS, MODE_PRIVATE).edit()
            .putBoolean(KEY_CONNECTED, connected)
        if (connected) {
            edit.putString(KEY_CONNECTED_HOST, host).putInt(KEY_CONNECTED_PORT, port)
        } else {
            edit.remove(KEY_CONNECTED_HOST).remove(KEY_CONNECTED_PORT)
        }
        edit.apply()
        val text = if (connected) "Connected to $host:$port" else "Searching…"
        getSystemService(NotificationManager::class.java)
            .notify(SVC_NOTIF_ID, buildServiceNotification(text))
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

    private fun registerNetworkCallback() {
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                // Only restart if we have no live connection — otherwise a secondary
                // network becoming available (mobile data, VPN, etc.) would tear down
                // a perfectly good WiFi WebSocket every 5 seconds.
                if (webSocket == null) {
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
        (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager)
            .registerDefaultNetworkCallback(cb)
        networkCallback = cb
    }

    private fun unregisterNetworkCallback() {
        networkCallback?.let {
            try {
                (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager)
                    .unregisterNetworkCallback(it)
            } catch (_: Exception) {}
        }
        networkCallback = null
    }

    private fun scheduleReconnect() {
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

    private fun buildServiceNotification(text: String) =
        NotificationCompat.Builder(this, SVC_CHANNEL_ID)
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
