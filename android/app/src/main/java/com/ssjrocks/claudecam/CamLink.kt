package com.ssjrocks.claudecam

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONException
import org.json.JSONObject
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetAddress
import java.nio.ByteBuffer
import java.util.concurrent.TimeUnit
import kotlin.math.min

/**
 * The WebSocket link to the Claude Cam server: finds the server (saved address, the address
 * baked in at build time, or mDNS), keeps reconnecting, and carries frames and commands.
 *
 * All state changes and listener calls happen on the main thread. The send methods are safe
 * to call from any thread.
 */
class CamLink(context: Context, private val listener: Listener) {

    interface Listener {
        fun onLinkState(state: State, detail: String)
        fun onCommand(cmd: JSONObject)
    }

    enum class State { SEARCHING, CONNECTING, CONNECTED, DISCONNECTED, REPLACED }

    private val prefs = context.getSharedPreferences("claudecam", Context.MODE_PRIVATE)
    private val main = Handler(Looper.getMainLooper())
    private val nsd = context.getSystemService(NsdManager::class.java)
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .pingInterval(10, TimeUnit.SECONDS)
        .build()

    @Volatile private var socket: WebSocket? = null
    @Volatile var isConnected = false
        private set
    var target: String? = prefs.getString("server", null) ?: BuildConfig.DEFAULT_SERVER.ifBlank { null }
        private set
    val discovered = linkedSetOf<String>()

    private var running = false
    private var replaced = false
    private var attempt = 0
    private var discovery: NsdManager.DiscoveryListener? = null
    private val reconnect = Runnable { connect() }

    fun start() {
        running = true
        replaced = false
        attempt = 0
        startDiscovery()
        connect()
    }

    fun stop() {
        running = false
        main.removeCallbacks(reconnect)
        stopDiscovery()
        socket?.close(1000, "app closed")
        socket = null
        isConnected = false
    }

    /** Address typed by the user, e.g. "192.168.1.23:8777" or "http://pc:8777/". */
    fun setManualTarget(raw: String) {
        val t = normalize(raw) ?: return
        target = t
        prefs.edit().putString("server", t).apply()
        reconnectNow()
    }

    fun reconnectNow() {
        main.removeCallbacks(reconnect)
        socket?.cancel()
        socket = null
        isConnected = false
        replaced = false
        attempt = 0
        connect()
    }

    // --- sending (any thread) -------------------------------------------------------------------

    fun sendJson(o: JSONObject) {
        if (isConnected) socket?.send(o.toString())
    }

    fun sendError(req: String, message: String) {
        sendJson(JSONObject().put("type", "result").put("req", req).put("ok", false).put("error", message))
    }

    /** Binary frame: 4-byte big-endian header length, JSON header, JPEG bytes. */
    fun sendFrame(kind: String, req: String?, jpeg: ByteArray, ageMs: Long) {
        val ws = socket ?: return
        if (!isConnected) return
        val header = JSONObject().put("kind", kind).put("age_ms", ageMs)
        if (req != null) header.put("req", req)
        val h = header.toString().toByteArray()
        val buf = ByteBuffer.allocate(4 + h.size + jpeg.size).putInt(h.size).put(h).put(jpeg)
        ws.send(buf.array().toByteString())
    }

    /** Bytes waiting to go out; used to drop stream frames when Wi-Fi can't keep up. */
    fun queuedBytes(): Long = socket?.queueSize() ?: 0

    // --- connection ---------------------------------------------------------------------------

    private fun connect() {
        if (!running || socket != null || replaced) return
        val t = target
        if (t == null) {
            listener.onLinkState(State.SEARCHING, "")
            return
        }
        listener.onLinkState(State.CONNECTING, t)
        val request = try {
            Request.Builder().url("ws://$t/ws/device").build()
        } catch (e: IllegalArgumentException) {
            listener.onLinkState(State.DISCONNECTED, "bad address $t")
            return
        }
        socket = client.newWebSocket(request, SocketListener(t))
    }

    private inner class SocketListener(private val addr: String) : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            main.post {
                if (webSocket !== socket) return@post
                isConnected = true
                attempt = 0
                stopDiscovery()
                webSocket.send(hello().toString())
                listener.onLinkState(State.CONNECTED, addr)
            }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val cmd = try {
                JSONObject(text)
            } catch (e: JSONException) {
                return
            }
            main.post { if (webSocket === socket) listener.onCommand(cmd) }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            if (code == 4000) main.post { if (webSocket === socket) replaced = true }
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            main.post { dropped(webSocket, reason.ifBlank { "closed" }) }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            main.post { dropped(webSocket, t.message ?: t.javaClass.simpleName) }
        }
    }

    private fun dropped(ws: WebSocket, why: String) {
        if (ws !== socket) return
        socket = null
        isConnected = false
        if (!running) return
        if (replaced) {
            // Another phone took over. Don't fight it; the user can tap the status to reconnect.
            listener.onLinkState(State.REPLACED, why)
            return
        }
        listener.onLinkState(State.DISCONNECTED, why)
        startDiscovery()
        val delay = min(5000L, 500L shl min(attempt, 4))
        attempt++
        main.postDelayed(reconnect, delay)
    }

    private fun hello() = JSONObject()
        .put("type", "hello")
        .put("manufacturer", Build.MANUFACTURER.replaceFirstChar { it.uppercase() })
        .put("model", Build.MODEL)
        .put("android", Build.VERSION.RELEASE)
        .put("app_version", BuildConfig.VERSION_NAME)

    // --- mDNS discovery -----------------------------------------------------------------------

    private fun startDiscovery() {
        if (discovery != null || !running) return
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {}
            override fun onDiscoveryStopped(serviceType: String) {}
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                main.post { if (discovery === this) discovery = null }
            }
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
            override fun onServiceFound(info: NsdServiceInfo) = resolve(info, 0)
            override fun onServiceLost(info: NsdServiceInfo) {}
        }
        discovery = l
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: RuntimeException) {
            discovery = null
        }
    }

    private fun stopDiscovery() {
        val l = discovery ?: return
        discovery = null
        try {
            nsd.stopServiceDiscovery(l)
        } catch (e: RuntimeException) {
            // already stopped
        }
    }

    @Suppress("DEPRECATION") // resolveService is deprecated on API 34 but works everywhere
    private fun resolve(info: NsdServiceInfo, tries: Int) {
        nsd.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(si: NsdServiceInfo, errorCode: Int) {
                if (errorCode == NsdManager.FAILURE_ALREADY_ACTIVE && tries < 5) {
                    main.postDelayed({ if (discovery != null) resolve(info, tries + 1) }, 400)
                }
            }

            override fun onServiceResolved(si: NsdServiceInfo) {
                val host = hostOf(si) ?: return
                val addr = "$host:${si.port}"
                main.post { onDiscovered(addr) }
            }
        })
    }

    private fun onDiscovered(addr: String) {
        discovered.add(addr)
        if (isConnected || replaced) return
        // Switch away from the saved address only once it has failed, so discovery never
        // overrides a server the user typed in while that one is still connecting.
        if (addr != target && (target == null || attempt > 0)) {
            target = addr
            prefs.edit().putString("server", addr).apply()
            reconnectNow()
        } else if (socket == null) {
            main.removeCallbacks(reconnect)
            connect()
        }
    }

    private fun hostOf(si: NsdServiceInfo): String? {
        val addrs: List<InetAddress> = if (Build.VERSION.SDK_INT >= 34) {
            si.hostAddresses
        } else {
            @Suppress("DEPRECATION")
            listOfNotNull(si.host)
        }
        val a = addrs.firstOrNull { it is Inet4Address } ?: addrs.firstOrNull() ?: return null
        val text = a.hostAddress ?: return null
        return if (a is Inet6Address) "[${text.substringBefore('%')}]" else text
    }

    companion object {
        const val SERVICE_TYPE = "_claudecam._tcp"
        const val DEFAULT_PORT = 8777

        fun normalize(raw: String): String? {
            var s = raw.trim()
            for (prefix in listOf("ws://", "wss://", "http://", "https://")) s = s.removePrefix(prefix)
            s = s.substringBefore('/')
            if (s.isEmpty()) return null
            val hasPort = if (s.startsWith("[")) s.contains("]:") else s.contains(':')
            return if (hasPort) s else "$s:$DEFAULT_PORT"
        }
    }
}
