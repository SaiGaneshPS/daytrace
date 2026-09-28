// DT-22: the pairing store: the token kept encrypted, read back, forgotten, and a lost key means pairing again.
package app.daytrace.android.sync

import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class PairingStoreTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    /** Stands in for the Keystore (the JVM has none): XOR with a key, and a switch to lose the key. */
    private class FakeCipher : TokenCipher {
        var lost = false
        var decrypts = 0

        override fun encrypt(plain: ByteArray) = byteArrayOf(7) to plain.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()

        override fun decrypt(iv: ByteArray, sealed: ByteArray): ByteArray {
            check(!lost) { "key gone" }
            decrypts++
            return sealed.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
        }
    }

    private val cipher = FakeCipher()
    private val store = PairingStore(context, cipher)
    private val pairing = Pairing(HubConfig("http://192.168.1.23:8765", "dt_secret_token", "android-1"), "personal", "Galaxy S25 Ultra")

    @Test
    fun aSavedPairingComesBack() {
        assertNull(store.load())
        store.save(pairing)
        assertEquals(pairing, store.pairing())
        assertEquals(pairing.config, store.load())
        assertEquals(PairingInfo("http://192.168.1.23:8765", "android-1", "personal"), store.info())
    }

    @Test
    fun theDashboardTokenGoesWithThePairingEncryptedLikeTheOther() {
        val withDashboard = pairing.copy(viewerToken = "dt_viewer_token")
        store.save(withDashboard)
        assertEquals(withDashboard, store.pairing())
        assertEquals("dt_viewer_token", store.viewerToken())
        val saved = context.getSharedPreferences("pairing", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(saved, "dt_viewer_token" in saved)
        assertFalse("dt_viewer_token" in withDashboard.toString()) // never printed
        cipher.decrypts = 0
        assertEquals(withDashboard.config, store.load()) // what a sync reads: the dashboard token stays sealed
        assertEquals(1, cipher.decrypts)
        store.save(pairing) // paired again without one (an older hub): the old one goes
        assertNull(store.viewerToken())
        store.save(withDashboard)
        store.clear() // forgotten: gone too
        assertNull(store.viewerToken())
    }

    @Test
    fun theTokenIsNeverStoredInTheClear() {
        store.save(pairing)
        val saved = context.getSharedPreferences("pairing", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(saved, "dt_secret_token" in saved)
    }

    @Test
    fun forgettingTheHubStopsSyncingButRemembersWhoThePhoneWas() {
        store.save(pairing)
        store.clear()
        assertNull(store.load()) // nothing syncs any more
        assertNull(store.info())
        assertEquals(listOf(pairing.config), store.candidates()) // DT-22: pairing with it again keeps android-1
        val saved = context.getSharedPreferences("pairing", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(saved, "dt_secret_token" in saved) // set aside, still encrypted
    }

    @Test
    fun aTokenThatCannotBeDecryptedMeansPairingAgain() {
        store.save(pairing)
        cipher.lost = true
        assertNull(store.load())
    }

    @Test
    fun pairingAgainReplacesTheOldPairing() {
        store.save(pairing)
        val again = pairing.copy(config = HubConfig("http://192.168.1.30:8765", "dt_new", "android-2"))
        store.save(again)
        assertEquals(again, store.pairing())
    }

    // --- DT-22: the pairings kept so that pairing again keeps the phone's id ---

    private fun other(n: Int, profile: String = "demo") =
        Pairing(HubConfig("http://192.168.1.$n:8767", "dt_token_$n", "android-$n"), profile, "Phone")

    @Test
    fun theCurrentPairingComesFirstThenTheOnesSetAsideNewestFirst() {
        store.save(pairing)
        store.save(other(2))
        assertEquals(listOf(other(2).config, pairing.config), store.candidates())
        store.clear()
        assertEquals(listOf(other(2).config, pairing.config), store.candidates())
        assertNull(store.load())
    }

    @Test
    fun theSameDeviceOnTheSameProfileKeepsOnlyItsNewestToken() {
        store.save(pairing)
        val renewed = pairing.copy(config = pairing.config.copy(token = "dt_renewed")) // paired again, same id
        store.save(renewed)
        assertEquals(listOf(renewed.config), store.candidates()) // the old token no longer works: not kept
    }

    @Test
    fun onlyAFewPairingsAreKept() {
        (1..10).forEach { store.save(other(it)) }
        assertEquals(1 + PairingStore.MAX_FORMER, store.candidates().size)
        assertEquals(other(10).config, store.candidates().first())
    }

    @Test
    fun aDeadPairingIsDroppedNotKept() {
        store.save(pairing)
        store.discardCurrent()
        assertNull(store.load())
        assertEquals(emptyList<HubConfig>(), store.candidates())
        store.save(pairing)
        cipher.lost = true // the Keystore key is gone: nothing can be decrypted, nothing is offered
        assertEquals(emptyList<HubConfig>(), store.candidates())
    }
}
