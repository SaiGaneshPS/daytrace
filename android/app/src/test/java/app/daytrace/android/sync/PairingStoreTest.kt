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

        override fun encrypt(plain: ByteArray) = byteArrayOf(7) to plain.map { (it.toInt() xor 0x5A).toByte() }.toByteArray()

        override fun decrypt(iv: ByteArray, sealed: ByteArray): ByteArray {
            check(!lost) { "key gone" }
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
    fun theTokenIsNeverStoredInTheClear() {
        store.save(pairing)
        val saved = context.getSharedPreferences("pairing", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(saved, "dt_secret_token" in saved)
    }

    @Test
    fun forgettingTheHubRemovesEverything() {
        store.save(pairing)
        store.clear()
        assertNull(store.load())
        assertNull(store.info())
        assertTrue(context.getSharedPreferences("pairing", Context.MODE_PRIVATE).all.isEmpty())
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
}
