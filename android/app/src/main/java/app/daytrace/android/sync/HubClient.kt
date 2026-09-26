// DT-21: the hub's HTTP API (docs/api.md) over OkHttp, as this device with its Bearer token. It only ever talks to
// addresses on your own network: until DT-47 adds HTTPS, events and the token travel as plain HTTP, so they must
// never leave the LAN (see res/xml/network_security_config.xml).
package app.daytrace.android.sync

import app.daytrace.android.data.EventEntity
import app.daytrace.android.data.PhoneEvent
import okhttp3.Dns
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.InetAddress
import java.net.Proxy
import java.net.UnknownHostException
import java.security.MessageDigest
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/** How to reach the hub as this device. Pairing (DT-22) fills it in. */
data class HubConfig(val baseUrl: String, val token: String, val deviceId: String) {
    override fun toString() = "HubConfig(baseUrl=$baseUrl, deviceId=$deviceId)" // never print the token
}

fun interface HubConfigSource {
    fun load(): HubConfig?
}

/** An event the hub did not store, and why (docs/api.md, "rejected"). [index] is its place in the batch. */
data class Rejection(val index: Int, val code: String, val reason: String)

data class IngestReply(val rejected: List<Rejection>)

sealed interface HubResult<out T> {
    data class Ok<T>(val value: T) : HubResult<T>
    /** 413: send fewer events at a time. */
    data class Split(val message: String) : HubResult<Nothing>
    /** The token was revoked, or is not allowed to send events: pair again. */
    data class Unauthorized(val message: String) : HubResult<Nothing>
    /** The hub's address is not on a private network, so nothing was sent. */
    data class Blocked(val message: String) : HubResult<Nothing>
    /** The hub is off, busy or out of reach, or refused this network: try again later. */
    data class Retry(val message: String) : HubResult<Nothing>
}

class NotPrivateAddressException(host: String) :
    UnknownHostException("$host is not on your home network, so Daytrace will not send anything there")

/** Loopback, private LAN, link-local and Tailscale addresses: the only places the hub can be. */
object PrivateNetwork {
    fun isPrivate(address: ByteArray): Boolean = when (address.size) {
        4 -> isPrivateV4(address)
        16 -> isPrivateV6(address)
        else -> false
    }

    private fun isPrivateV4(a: ByteArray): Boolean {
        val first = a[0].toInt() and 0xff
        val second = a[1].toInt() and 0xff
        return first == 10 || first == 127 ||
            (first == 172 && second in 16..31) ||
            (first == 192 && second == 168) ||
            (first == 169 && second == 254) ||
            (first == 100 && second in 64..127) // Tailscale (carrier-grade NAT range)
    }

    private fun isPrivateV6(a: ByteArray): Boolean {
        val first = a[0].toInt() and 0xff
        val second = a[1].toInt() and 0xff
        val zeroPrefix = (0 until 10).all { a[it].toInt() == 0 }
        return when {
            zeroPrefix && (a[10].toInt() and 0xff) == 0xff && (a[11].toInt() and 0xff) == 0xff ->
                isPrivateV4(a.copyOfRange(12, 16)) // an IPv4 address written as IPv6 (::ffff:a.b.c.d)
            zeroPrefix && (10 until 15).all { a[it].toInt() == 0 } && a[15].toInt() == 1 -> true // ::1
            (first and 0xfe) == 0xfc -> true // fc00::/7, unique local (Tailscale uses fd7a:115c:a1e0::/48)
            first == 0xfe && (second and 0xc0) == 0x80 -> true // fe80::/10, link-local
            else -> false
        }
    }

    /**
     * A quick check before connecting. OkHttp connects to anything made of digits and dots without a lookup, and
     * the system reads short and octal forms too (134744072 and 010.8.8.8 are both 8.8.8.8), so only the plain
     * a.b.c.d form is accepted, and it must be private. Names are checked by [FilteringDns], and whatever
     * address is finally connected to by [ConnectedAddressCheck].
     */
    fun check(url: HttpUrl) {
        val host = url.host
        val literal = when {
            ':' in host -> InetAddress.getByName(host).address // IPv6: parsed, never looked up
            host.all { it.isDigit() || it == '.' } -> parseIpv4(host) ?: throw NotPrivateAddressException(host)
            else -> null
        }
        if (literal != null && !isPrivate(literal)) throw NotPrivateAddressException(host)
    }

    /** Four numbers 0 to 255 with no leading zeros, or null. */
    private fun parseIpv4(host: String): ByteArray? {
        val parts = host.split('.')
        if (parts.size != 4) return null
        val numbers = parts.map { part ->
            val plain = part.length in 1..3 && part.all(Char::isDigit) && (part == "0" || !part.startsWith('0'))
            part.takeIf { plain }?.toInt()?.takeIf { it <= 255 } ?: return null
        }
        return ByteArray(4) { numbers[it].toByte() }
    }

    /** Resolves names as usual but keeps only private addresses; a name with none left fails to resolve. */
    class FilteringDns(private val delegate: Dns = Dns.SYSTEM) : Dns {
        override fun lookup(hostname: String): List<InetAddress> =
            delegate.lookup(hostname).filter { isPrivate(it.address) }.ifEmpty { throw NotPrivateAddressException(hostname) }
    }

    /**
     * The last word: checks the address the connection actually reached, after connecting and before a single
     * byte of the request (the token included) is written, however the host was spelled or resolved.
     */
    class ConnectedAddressCheck(private val allowed: (InetAddress) -> Boolean = { isPrivate(it.address) }) : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val peer = chain.connection()?.socket()?.inetAddress
            if (peer == null || !allowed(peer)) throw NotPrivateAddressException(peer?.hostAddress ?: chain.request().url.host)
            return chain.proceed(chain.request())
        }
    }
}

class HubClient(private val config: HubConfig, private val http: OkHttpClient = defaultHttp) {
    /** GET /devices/{id}/cursor: the highest seq the hub has for this device, or null when it has none. */
    fun cursor(): HubResult<Long?> {
        val url = apiUrl("devices")?.newBuilder()?.addPathSegment(config.deviceId)?.addPathSegment("cursor")?.build()
            ?: return badUrl()
        return call(Request.Builder().url(url).get()) { body ->
            val json = JSONObject(body)
            if (json.isNull("last_seq")) null else json.getLong("last_seq")
        }
    }

    /** POST /events with these events, in this order (at most 500; the sync sends 200 at a time). */
    fun send(events: List<EventEntity>): HubResult<IngestReply> {
        val url = apiUrl("events") ?: return badUrl()
        val body = JSONObject().put("events", JSONArray(events.map { it.toHubJson(config.deviceId) })).toString()
        return call(Request.Builder().url(url).post(body.toRequestBody(JSON))) { text ->
            val reply = JSONObject(text)
            val rejected = reply.optJSONArray("rejected") ?: JSONArray()
            IngestReply(
                (0 until rejected.length()).map { rejected.getJSONObject(it) }.map {
                    Rejection(it.getInt("index"), it.optString("code", "invalid"), it.optString("reason"))
                },
            )
        }
    }

    private fun apiUrl(path: String): HttpUrl? {
        val base = config.baseUrl.toHttpUrlOrNull() ?: return null
        return base.newBuilder().addPathSegments("api/v1/$path").build()
    }

    private fun badUrl() = HubResult.Blocked("\"${config.baseUrl}\" is not a web address Daytrace can use")

    private fun <T> call(request: Request.Builder, parse: (String) -> T): HubResult<T> {
        val built = request.header("Authorization", "Bearer ${config.token}").header("Accept", "application/json").build()
        return try {
            PrivateNetwork.check(built.url)
            http.newCall(built).execute().use { response ->
                val text = response.body.string()
                val error = errorOf(text)
                when {
                    response.code == 200 -> runCatching { HubResult.Ok(parse(text)) }
                        .getOrElse { HubResult.Retry("The hub sent a reply Daytrace doesn't understand") }
                    response.code == 413 -> HubResult.Split(error?.second ?: "HTTP 413")
                    response.code == 401 || (response.code == 403 && error?.first == "forbidden") ->
                        HubResult.Unauthorized(error?.second ?: "HTTP ${response.code}")
                    else -> HubResult.Retry(error?.second ?: "HTTP ${response.code}")
                }
            }
        } catch (e: IOException) {
            val blocked = generateSequence<Throwable>(e) { it.cause }.firstOrNull { it is NotPrivateAddressException }
            if (blocked != null) HubResult.Blocked(blocked.message.orEmpty()) else HubResult.Retry(e.message ?: e.javaClass.simpleName)
        }
    }

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
        const val MAX_TEXT = 200 // the hub's limit for app and app_id
        private const val ZERO_WIDTH_JOINER = 0x200D

        /**
         * No proxy (a proxy would take the traffic off the LAN and do the lookup itself), no redirects, every
         * connection checked before the request is written, and short timeouts: the hub is nearby.
         */
        fun httpClient(
            dns: Dns = PrivateNetwork.FilteringDns(),
            connected: Interceptor = PrivateNetwork.ConnectedAddressCheck(),
        ): OkHttpClient = OkHttpClient.Builder()
            .dns(dns)
            .addNetworkInterceptor(connected)
            .proxy(Proxy.NO_PROXY)
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(60, TimeUnit.SECONDS)
            .build()

        private val defaultHttp by lazy { httpClient() }

        /** {"error": {"code", "message"}} from the hub, or null for any other body. */
        private fun errorOf(text: String): Pair<String, String>? = runCatching {
            val error = JSONObject(text).getJSONObject("error")
            error.getString("code") to error.getString("message")
        }.getOrNull()

        /** The key as the hub's external_id (printable ASCII, at most 200), or a hash of it when it is not one. */
        fun externalId(key: String): String =
            if (key.length in 1..200 && key.all { it in '!'..'~' }) {
                key
            } else {
                "sha256:" + MessageDigest.getInstance("SHA-256").digest(key.toByteArray()).joinToString("") { "%02x".format(it) }
            }

        /**
         * App names as the hub stores them: control and formatting characters removed (one could flip the text
         * in a list), no broken emoji halves, and at most [max] characters without cutting an emoji in two.
         */
        fun cleanText(text: String, max: Int = MAX_TEXT): String? {
            val out = StringBuilder()
            var kept = 0
            var i = 0
            while (i < text.length && kept < max) {
                val codePoint = text.codePointAt(i)
                val type = Character.getType(codePoint)
                val hidden = type == Character.CONTROL.toInt() || type == Character.SURROGATE.toInt() ||
                    (type == Character.FORMAT.toInt() && codePoint != ZERO_WIDTH_JOINER)
                if (!hidden) {
                    out.appendCodePoint(codePoint)
                    kept++
                }
                i += Character.charCount(codePoint)
            }
            return out.toString().trim().ifEmpty { null }
        }

        fun EventEntity.toHubJson(deviceId: String): JSONObject {
            val zone = runCatching { ZoneId.of(zone) }.getOrElse { ZoneId.systemDefault() }
            return JSONObject()
                .put("device_id", deviceId)
                .put("seq", seq)
                .put("external_id", externalId(key))
                .put("kind", kind)
                .put("source", source)
                .put("start", PhoneEvent.iso(startMs, zone))
                .putOpt("end", endMs?.let { PhoneEvent.iso(it, zone) })
                .putOpt("app", app?.let { cleanText(it) })
                .putOpt("app_id", appId?.let { cleanText(it) })
        }
    }
}
