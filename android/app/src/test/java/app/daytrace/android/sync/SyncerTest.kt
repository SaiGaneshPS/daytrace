// DT-21 / DT-22: the sync loop against a fake hub: exactly once, in batches, numbered above the hub, never stuck,
// and the token only ever sent to a hub that proved it paired this phone.
package app.daytrace.android.sync

import android.app.Application
import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.WorkManager
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import app.daytrace.android.data.AppDatabase
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.data.StoreCounts
import kotlinx.coroutines.runBlocking
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.QueueDispatcher
import mockwebserver3.RecordedRequest
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class SyncerTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val server = MockWebServer()
    private lateinit var db: AppDatabase
    private lateinit var store: EventStore
    private val status = SyncStatusStore(context)
    private var hub: HubConfig? = null

    /** Answers the hub proof from the nonce it is sent (as the real hub does), and everything else from a queue. */
    private inner class FakeHub : Dispatcher() {
        val queue = QueueDispatcher().apply { setFailFast(true) }
        var proofToken: String? = null // null: the paired token, so the proof is right
        var proofStatus = 200

        override fun dispatch(request: RecordedRequest): MockResponse {
            if (!isProof(request)) return queue.dispatch(request)
            if (proofStatus != 200) return reply(proofStatus, """{"error": {"code": "unauthorized", "message": "pair it again"}}""")
            val nonce = JSONObject(request.body!!.utf8()).getString("nonce")
            val proof = HubClient.expectedProof(proofToken ?: hub!!.token, nonce)
            return reply(200, """{"device_id": "${hub!!.deviceId}", "proof": "$proof"}""")
        }
    }

    private val fakeHub = FakeHub()
    private fun isProof(request: RecordedRequest) = request.url.encodedPath.endsWith("/proof")
    private fun enqueue(response: MockResponse) = fakeHub.queue.enqueue(response)

    /** The next request that is not a proof check. */
    private fun next(): RecordedRequest {
        while (true) server.takeRequest().let { if (!isProof(it)) return it }
    }

    @Before
    fun setUp() {
        server.dispatcher = fakeHub
        server.start()
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).allowMainThreadQueries().build()
        store = EventStore(db)
        hub = HubConfig(server.url("/").toString(), "dt_secret", "android-1")
    }

    @After
    fun tearDown() {
        db.close()
        server.close()
    }

    private var onWifi = true
    private fun sync() = runBlocking {
        Syncer(store, { hub }, status, wifi = { if (onWifi) HubClient.httpClient() else null }, clock = { 1_000L }).sync()
    }

    private fun collect(count: Int, from: Long = 0) =
        store.add((0 until count).map { PhoneEvent("app_session", "usagestats", (from + it) * 1_000, (from + it) * 1_000 + 500, "YouTube", "yt") })

    private fun reply(code: Int, body: String) = MockResponse.Builder().code(code).body(body).build()
    private fun cursor(lastSeq: Long?) = reply(200, """{"device_id": "android-1", "last_seq": $lastSeq}""")
    private fun stored(lastSeq: Long, rejected: String = "") =
        reply(200, """{"accepted": 1, "replaced": 0, "duplicates": 0, "rejected": [$rejected], "last_seq": $lastSeq}""")
    private fun seqsIn(request: RecordedRequest): List<Long> {
        val events = JSONObject(request.body!!.utf8()).getJSONArray("events")
        return (0 until events.length()).map { events.getJSONObject(it).getLong("seq") }
    }

    @Test
    fun somethingThatCannotProveItIsTheHubNeverGetsTheToken() {
        collect(1)
        fakeHub.proofToken = "dt_someone_else" // a stranger's device at the hub's address
        val report = sync()
        assertEquals(SyncResult.BLOCKED, report.result)
        assertTrue(report.message, "could not prove" in report.message)
        val proof = server.takeRequest()
        assertTrue(isProof(proof))
        assertEquals(null, proof.headers["Authorization"])
        assertEquals(1, server.requestCount) // nothing after the failed proof
        assertEquals(1, store.counts().waiting)
    }

    @Test
    fun offWifiNothingIsSentAndTheScreenSaysWhy() {
        collect(1)
        onWifi = false // on cellular, or no network at all
        assertEquals(SyncReport(SyncResult.NOT_ON_WIFI, WifiOnly.WAITING), sync())
        assertEquals(0, server.requestCount)
        assertEquals(1, store.counts().waiting)
    }

    @Test
    fun aRevokedPairingIsNoticedBeforeTheTokenIsSent() {
        collect(1)
        fakeHub.proofStatus = 401
        assertEquals(SyncResult.PAIR_AGAIN, sync().result)
        assertEquals(1, server.requestCount)
        assertEquals(null, server.takeRequest().headers["Authorization"])
    }

    @Test
    fun withoutPairingNothingIsSent() {
        collect(2)
        hub = null
        assertEquals(SyncResult.NOT_PAIRED, sync().result)
        assertEquals(0, server.requestCount)
        assertEquals(2, store.counts().waiting)
    }

    @Test
    fun theFirstSyncReadsTheCursorAndNumbersAboveTheHub() {
        collect(3)
        enqueue(cursor(41))
        enqueue(stored(44))
        val report = sync()
        assertEquals(SyncReport(SyncResult.SENT, "Sent 3 events to your hub", sent = 3), report)
        assertEquals("/api/v1/devices/android-1/cursor", next().url.encodedPath)
        assertEquals(listOf(42L, 43L, 44L), seqsIn(next()))
        assertEquals(StoreCounts(waiting = 0, refused = 0), store.counts())
        assertEquals(1_000L, status.read().lastSuccessMs)

        collect(1, from = 10) // later syncs go straight to sending
        enqueue(stored(45))
        sync()
        assertEquals(listOf(45L), seqsIn(next()))
    }

    @Test
    fun manyEventsGoInBatchesOf200() {
        collect(450)
        enqueue(cursor(null))
        repeat(3) { enqueue(stored(449)) }
        assertEquals(450, sync().sent)
        next()
        assertEquals(listOf(200, 200, 50), List(3) { seqsIn(next()).size })
    }

    @Test
    fun aBatchTooLargeForTheHubIsHalved() {
        collect(3)
        enqueue(cursor(null))
        enqueue(reply(413, """{"error": {"code": "body_too_large", "message": "too big"}}"""))
        enqueue(stored(0))
        enqueue(stored(2))
        assertEquals(3, sync().sent)
        next()
        assertEquals(listOf(listOf(0L, 1L, 2L), listOf(0L), listOf(1L, 2L)), List(3) { seqsIn(next()) })
    }

    @Test
    fun a413ForASingleEventStopsAndKeepsIt() {
        // One event always fits in a request, so this is not the hub talking: nothing may be refused for good.
        collect(1)
        enqueue(cursor(null))
        enqueue(reply(413, """{"error": {"code": "body_too_large", "message": "too big"}}"""))
        assertEquals(SyncResult.UNREACHABLE, sync().result)
        assertEquals(StoreCounts(waiting = 1, refused = 0), store.counts())
    }

    @Test
    fun a400StopsWithoutRefusingAnything() {
        collect(300)
        enqueue(cursor(null))
        enqueue(reply(400, """{"error": {"code": "bad_request", "message": "not what I expected"}}"""))
        assertEquals(SyncResult.UNREACHABLE, sync().result)
        assertEquals(3, server.requestCount) // the proof, the cursor and one batch: no halving down to single events
        assertEquals(StoreCounts(waiting = 300, refused = 0), store.counts())
    }

    @Test
    fun refusedEventsAreSetAsideAndTheRestMarkedSent() {
        collect(3)
        enqueue(cursor(null))
        enqueue(stored(2, """{"index": 1, "code": "invalid", "seq": 1, "reason": "end before start"}"""))
        val report = sync()
        assertEquals(2, report.sent)
        assertEquals(1, report.refused)
        assertEquals(StoreCounts(waiting = 0, refused = 1), store.counts())
    }

    @Test
    fun anyOtherRefusalIsSetAsideAndReportedAsSuch() {
        collect(1)
        enqueue(cursor(null))
        enqueue(stored(0, """{"index": 0, "code": "seq_conflict", "seq": 0, "reason": "seq 0 was already used"}"""))
        assertEquals(SyncReport(SyncResult.SENT, "Your hub refused 1 event", sent = 0, refused = 1), sync())
        assertEquals(StoreCounts(waiting = 0, refused = 1), store.counts())
    }

    @Test
    fun pairingAsAnotherDeviceReadsTheCursorAgain() {
        collect(1)
        enqueue(cursor(null))
        enqueue(stored(0))
        sync()
        hub = HubConfig(server.url("/").toString(), "dt_other", "android-2")
        collect(1, from = 10)
        enqueue(cursor(null))
        enqueue(stored(1))
        sync()
        val paths = List(4) { next().url.encodedPath }
        assertEquals(
            listOf("/api/v1/devices/android-1/cursor", "/api/v1/events", "/api/v1/devices/android-2/cursor", "/api/v1/events"),
            paths,
        )
    }

    @Test
    fun aRevokedTokenStopsAndKeepsEverything() {
        collect(2)
        enqueue(cursor(null))
        enqueue(reply(401, """{"error": {"code": "unauthorized", "message": "revoked"}}"""))
        assertEquals(SyncResult.PAIR_AGAIN, sync().result)
        assertEquals(2, store.counts().waiting)
    }

    @Test
    fun aTokenForAnotherDeviceStopsAndKeepsEverything() {
        collect(2)
        enqueue(cursor(null))
        enqueue(stored(0, """{"index": 0, "code": "wrong_device", "seq": 0, "reason": "device_id is android-2"}"""))
        assertEquals(SyncResult.PAIR_AGAIN, sync().result)
        assertEquals(1, store.counts().waiting)
    }

    @Test
    fun aBusyHubGetsTheSameEventsAgainSoItCountsThemOnce() {
        collect(2)
        enqueue(cursor(null))
        enqueue(reply(503, """{"error": {"code": "busy", "message": "busy"}}"""))
        assertEquals(SyncResult.UNREACHABLE, sync().result)
        assertEquals(null, status.read().lastSuccessMs)
        enqueue(stored(1))
        assertEquals(SyncResult.SENT, sync().result)
        next()
        val first = next().body!!.utf8()
        assertEquals(first, next().body!!.utf8()) // same seqs and external ids: the hub keeps one copy
    }

    @Test
    fun aHubThatIsOffIsTriedAgainLater() {
        collect(1)
        hub = HubConfig("http://127.0.0.1:1", "dt_secret", "android-1")
        assertEquals(SyncResult.UNREACHABLE, sync().result)
        assertEquals(1, store.counts().waiting)
    }

    @Test
    fun aHubAddressOnThePublicInternetIsRefused() {
        collect(1)
        hub = HubConfig("http://8.8.8.8:8765", "dt_secret", "android-1")
        val report = sync()
        assertEquals(SyncResult.BLOCKED, report.result)
        assertTrue(report.message, "home network" in report.message)
    }

    @Test
    fun theWorkerFinishesQuietlyWhenNotPaired() {
        val worker = TestListenableWorkerBuilder<SyncWorker>(context).build()
        assertEquals(ListenableWorker.Result.success(), runBlocking { worker.doWork() })
    }

    @Test
    fun backgroundSyncIsScheduledEvery15MinutesOnUnmeteredNetworks() {
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        SyncWorker.schedule(context)
        SyncWorker.schedule(context) // scheduling again keeps the first one
        val infos = WorkManager.getInstance(context).getWorkInfosForUniqueWork(SyncWorker.PERIODIC).get()
        assertEquals(1, infos.size)
        assertEquals(NetworkType.UNMETERED, infos.single().constraints.requiredNetworkType)
        assertEquals(15 * 60_000L, infos.single().periodicityInfo!!.repeatIntervalMillis)
    }
}
