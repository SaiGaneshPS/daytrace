// DT-21 / DT-22: the hub client: the requests the hub expects, every kind of answer, pairing, the hub's proof, and
// nothing sent off the home network.
package app.daytrace.android.sync

import app.daytrace.android.data.EventEntity
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Dns
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetAddress

class HubClientTest {
    private val server = MockWebServer()

    @Before
    fun start() = server.start()

    @After
    fun stop() = server.close()

    private val http = HubClient.httpClient()
    private fun client(baseUrl: String = server.url("/").toString()) = HubClient(HubConfig(baseUrl, "dt_secret", "android-1"), http)
    private fun reply(code: Int, body: String = "") = MockResponse.Builder().code(code).body(body).build()
    private fun error(code: String) = """{"error": {"code": "$code", "message": "the hub says $code", "details": []}}"""
    private fun event(seq: Long, app: String? = "YouTube") = EventEntity(
        id = seq + 1, seq = seq, key = "app_session|1790307924094|com.google.android.youtube", kind = "app_session",
        source = "usagestats", startMs = 1_790_307_924_094, endMs = 1_790_307_984_094, app = app,
        appId = "com.google.android.youtube", zone = "America/St_Johns",
    )

    // --- pairing and the hub's proof (DT-22) ---

    @Test
    fun theProofIsComputedExactlyLikeTheHubDoes() {
        // Worked out in Python the way hub/daytrace_hub/api/devices.py does it (hmac over sha256(token).hexdigest()).
        assertEquals(
            "abf47d9c4a9d458178268f0ae60604730397727cc2a4ee8faf2b8bb6e7bf27f1",
            HubClient.expectedProof("dt_secret", "0123456789abcdef0123456789abcdef"),
        )
        assertTrue(HubClient.randomNonce().matches(Regex("[0-9a-f]{32}")))
        assertTrue(HubClient.randomNonce() != HubClient.randomNonce())
    }

    private val nonce = "0123456789abcdef0123456789abcdef"
    private fun prover() = HubClient(HubConfig(server.url("/").toString(), "dt_secret", "android-1"), http, newNonce = { nonce })
    private fun proof(token: String, message: String, revoked: Boolean = false) =
        reply(200, """{"device_id": "android-1", "revoked": $revoked, "proof": "${HubClient.expectedProof(token, message)}"}""")

    @Test
    fun theHubProvesItPairedThisPhoneAndTheTokenStaysHome() {
        server.enqueue(proof("dt_secret", nonce))
        server.enqueue(proof("dt_other", nonce)) // something else answered at the hub's address
        assertEquals(HubResult.Ok(HubProof.PAIRED), prover().proveHub())
        assertEquals(HubResult.Ok(HubProof.NOT_PROVEN), prover().proveHub())

        val request = server.takeRequest()
        assertEquals("/api/v1/devices/android-1/proof", request.url.encodedPath)
        assertEquals(nonce, JSONObject(request.body!!.utf8()).getString("nonce"))
        assertEquals(null, request.headers["Authorization"])
        assertTrue("dt_secret" !in request.body!!.utf8())
    }

    @Test
    fun onlyASignedRevocationMeansPairAgain() {
        server.enqueue(proof("dt_secret", "revoked:$nonce", revoked = true)) // our hub, signed
        server.enqueue(proof("dt_other", "revoked:$nonce", revoked = true)) // "revoked", signed by someone else
        server.enqueue(proof("dt_secret", nonce, revoked = true)) // "revoked" with a normal proof: not a revocation
        server.enqueue(reply(401, error("unauthorized"))) // unsigned: could come from anyone
        assertEquals(HubResult.Ok(HubProof.REVOKED), prover().proveHub())
        repeat(3) { assertEquals(HubResult.Ok(HubProof.NOT_PROVEN), prover().proveHub()) }
    }

    @Test
    fun aHubTooOldToProveItselfSaysSo() {
        server.enqueue(reply(404, error("not_found")))
        val result = prover().proveHub()
        assertTrue(result.toString(), result is HubResult.Retry && "needs an update" in result.message)
    }

    @Test
    fun hugeRepliesAreNotReadAndErrorTextIsCleaned() {
        server.enqueue(reply(200, "x".repeat(1_100_000)))
        assertEquals(HubResult.Retry("The reply was far too large for a Daytrace hub"), prover().proveHub())
        val rlo = String(Character.toChars(0x202E))
        server.enqueue(reply(503, """{"error": {"code": "busy", "message": "${rlo}moc.live ${"a".repeat(300)}"}}"""))
        val message = (prover().proveHub() as HubResult.Retry).message
        assertTrue(rlo !in message)
        assertEquals(200, message.length)
    }

    @Test
    fun pairingTradesTheCodeForAToken() {
        server.enqueue(
            reply(201, """{"device_id": "android-1", "device_type": "android", "name": "Galaxy S25 Ultra", "token": "dt_new", "profile": "personal"}"""),
        )
        val result = HubClient.claim(server.url("/").toString(), "493817", "Galaxy S25 Ultra", http)
        assertEquals(HubResult.Ok(Paired("android-1", "dt_new", "personal", "Galaxy S25 Ultra")), result)
        val request = server.takeRequest()
        assertEquals("/api/v1/pair/claim", request.url.encodedPath)
        assertEquals(null, request.headers["Authorization"])
        val body = JSONObject(request.body!!.utf8())
        assertEquals(listOf("493817", "Galaxy S25 Ultra", "android"), listOf("code", "device_name", "device_type").map(body::getString))
        assertTrue("dt_new" !in result.toString()) // never printed
    }

    @Test
    fun aWrongCodeComesBackWithTheHubsMessage() {
        server.enqueue(reply(400, """{"error": {"code": "invalid_code", "message": "wrong code, 4 tries left"}}"""))
        assertEquals(HubResult.Retry("wrong code, 4 tries left"), HubClient.claim(server.url("/").toString(), "000000", "Phone", http))
        assertTrue(HubClient.claim("http://8.8.8.8:8765", "493817", "Phone", http) is HubResult.Blocked)
    }

    // --- events ---

    @Test
    fun sendsTheBatchAsThisDeviceWithItsToken() {
        server.enqueue(reply(200, """{"accepted": 1, "replaced": 0, "duplicates": 0, "rejected": [], "last_seq": 7}"""))
        assertEquals(HubResult.Ok(IngestReply(emptyList())), client().send(listOf(event(7))))

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/events", request.url.encodedPath)
        assertEquals("Bearer dt_secret", request.headers["Authorization"])
        val sent = JSONObject(request.body!!.utf8()).getJSONArray("events").getJSONObject(0)
        assertEquals("android-1", sent.getString("device_id"))
        assertEquals(7L, sent.getLong("seq"))
        assertEquals("app_session|1790307924094|com.google.android.youtube", sent.getString("external_id"))
        assertEquals("2026-09-25T01:15:24.094-02:30", sent.getString("start")) // the offset where it happened
        assertEquals("2026-09-25T01:16:24.094-02:30", sent.getString("end"))
        assertEquals("YouTube", sent.getString("app"))
    }

    @Test
    fun refusedEventsComeBackWithTheirPlaceInTheBatch() {
        server.enqueue(
            reply(200, """{"accepted": 1, "rejected": [{"index": 1, "code": "invalid", "seq": 2, "external_id": null, "reason": "bad end"}], "last_seq": 1}"""),
        )
        val result = client().send(listOf(event(1), event(2)))
        assertEquals(HubResult.Ok(IngestReply(listOf(Rejection(1, "invalid", "bad end")))), result)
    }

    @Test
    fun everyAnswerMapsToWhatTheSyncShouldDo() {
        val answers = listOf(
            reply(401, error("unauthorized")) to HubResult.Unauthorized::class,
            reply(403, error("forbidden")) to HubResult.Unauthorized::class, // a viewer token
            reply(403, error("forbidden_network")) to HubResult.Retry::class, // the hub does not serve this Wi-Fi
            reply(413, error("body_too_large")) to HubResult.Split::class,
            reply(400, error("bad_request")) to HubResult.Retry::class, // not about the batch size
            reply(503, error("busy")) to HubResult.Retry::class,
            reply(200, "not json") to HubResult.Retry::class,
        )
        for ((answer, expected) in answers) {
            server.enqueue(answer)
            val result = client().send(listOf(event(1)))
            assertEquals("for ${answer.code}", expected, result::class)
        }
        server.enqueue(reply(503, error("busy")))
        assertEquals(HubResult.Retry("the hub says busy"), client().send(listOf(event(1))))
    }

    @Test
    fun redirectsAreNeverFollowed() {
        server.enqueue(MockResponse.Builder().code(302).setHeader("Location", "http://8.8.8.8/api/v1/events").build())
        assertEquals(HubResult.Retry("HTTP 302"), client().send(listOf(event(1))))
        assertEquals(1, server.requestCount)
    }

    @Test
    fun theCursorIsTheHubsHighestSeq() {
        server.enqueue(reply(200, """{"device_id": "android-1", "last_seq": 41}"""))
        server.enqueue(reply(200, """{"device_id": "android-1", "last_seq": null}"""))
        assertEquals(HubResult.Ok(41L), client().cursor())
        assertEquals(HubResult.Ok(null), client().cursor())
        assertEquals("/api/v1/devices/android-1/cursor", server.takeRequest().url.encodedPath)
    }

    @Test
    fun aHubThatIsOffMeansTryAgainLater() {
        assertTrue(client("http://127.0.0.1:1").send(listOf(event(1))) is HubResult.Retry)
    }

    @Test
    fun anAddressOutsideTheHomeNetworkIsNeverContacted() {
        val public = listOf(
            "http://8.8.8.8:8765", "http://[2001:4860:4860::8888]:8765", "http://100.128.0.1:8765",
            // other spellings the system reads as an IP: 8.8.8.8 as one number, 8.8.0.8, octal 010 = 8
            "http://134744072:8765", "http://8.8.8:8765", "http://010.8.8.8:8765", "http://192.168.001.5:8765",
        )
        for (url in public) {
            assertTrue(url, client(url).send(listOf(event(1))) is HubResult.Blocked)
        }
        assertTrue(client("not a url").cursor() is HubResult.Blocked)
    }

    @Test
    fun theAddressActuallyConnectedToIsCheckedBeforeAnythingIsSent() {
        val refuseAll = HubClient.httpClient(connected = PrivateNetwork.ConnectedAddressCheck { false })
        val hub = HubClient(HubConfig(server.url("/").toString(), "dt_secret", "android-1"), refuseAll)
        assertTrue(hub.send(listOf(event(1))) is HubResult.Blocked)
        assertEquals(0, server.requestCount) // connected, but not one byte of the request (or the token) was written
    }

    @Test
    fun aNameThatPointsOutsideTheHomeNetworkIsBlocked() {
        val publicDns = object : Dns {
            override fun lookup(hostname: String) = listOf(InetAddress.getByAddress(hostname, byteArrayOf(93, 184.toByte(), 215.toByte(), 14)))
        }
        val hub = HubClient(HubConfig("http://hub.example:8765", "dt_secret", "android-1"), HubClient.httpClient(PrivateNetwork.FilteringDns(publicDns)))
        assertTrue(hub.send(listOf(event(1))) is HubResult.Blocked)
    }

    @Test
    fun aNameWithPublicAndPrivateAddressesKeepsOnlyThePrivateOnes() {
        val mixed = object : Dns {
            override fun lookup(hostname: String) = listOf(
                InetAddress.getByAddress(hostname, byteArrayOf(93, 184.toByte(), 215.toByte(), 14)),
                InetAddress.getByAddress(hostname, byteArrayOf(192.toByte(), 168.toByte(), 1, 20)),
            )
        }
        assertEquals(listOf("192.168.1.20"), PrivateNetwork.FilteringDns(mixed).lookup("pc.local").map { it.hostAddress })
    }

    @Test
    fun onlyLoopbackLanLinkLocalAndTailscaleAddressesArePrivate() {
        val private = listOf(
            "10.0.0.1", "127.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.5", "169.254.10.1", "100.64.0.1",
            "100.127.255.254", "::1", "fe80::1", "fd7a:115c:a1e0::1", "fc00::1",
        )
        val public = listOf(
            "8.8.8.8", "172.32.0.1", "172.15.0.1", "192.169.0.1", "100.128.0.1", "100.63.255.255", "0.0.0.0",
            "2001:4860:4860::8888", "fec0::1", "::",
        )
        private.forEach { assertTrue(it, PrivateNetwork.isPrivate(InetAddress.getByName(it).address)) }
        public.forEach { assertTrue(it, !PrivateNetwork.isPrivate(InetAddress.getByName(it).address)) }
        val mappedPublic = ByteArray(16).also { it[10] = -1; it[11] = -1; it[12] = 8; it[13] = 8; it[14] = 8; it[15] = 8 }
        val mappedPrivate = ByteArray(16).also { it[10] = -1; it[11] = -1; it[12] = 192.toByte(); it[13] = 168.toByte(); it[15] = 1 }
        assertTrue(!PrivateNetwork.isPrivate(mappedPublic))
        assertTrue(PrivateNetwork.isPrivate(mappedPrivate))
    }

    @Test
    fun appNamesAreCleanedTheWayTheHubStoresThem() {
        val rlo = String(Character.toChars(0x202E))
        val zwj = String(Character.toChars(0x200D))
        val laptopPerson = String(Character.toChars(0x1F9D1)) + zwj + String(Character.toChars(0x1F4BB))
        assertEquals("evil.exe", HubClient.cleanText("evil${rlo}.exe"))
        assertEquals(laptopPerson, HubClient.cleanText(laptopPerson)) // the joiner stays: emoji need it
        assertEquals(null, HubClient.cleanText("$rlo "))
        val longEmoji = String(Character.toChars(0x1F600)).repeat(250)
        assertEquals(200, HubClient.cleanText(longEmoji)!!.codePointCount(0, 400)) // 200 characters, none cut in half
    }

    @Test
    fun keysThatAreNotPlainAsciiBecomeAHash() {
        assertEquals("screen_on|5|", HubClient.externalId("screen_on|5|"))
        val hashed = HubClient.externalId("app_session|5|My App")
        assertTrue(hashed, hashed.matches(Regex("sha256:[0-9a-f]{64}")))
        assertEquals(hashed, HubClient.externalId("app_session|5|My App")) // the same key, the same id
    }
}
