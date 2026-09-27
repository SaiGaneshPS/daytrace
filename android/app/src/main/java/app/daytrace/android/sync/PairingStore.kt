// DT-21 / DT-22: where the hub address, the device id and the token are kept. The token is encrypted with an AES
// key that lives in the Android Keystore (it cannot be read out of the phone), and the app's data is never backed
// up (AndroidManifest.xml), so the token stays on this phone.
package app.daytrace.android.sync

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.content.edit
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Encrypts the token. Tests use a stand-in, since the JVM has no Android Keystore. */
interface TokenCipher {
    /** Returns the IV and the ciphertext. */
    fun encrypt(plain: ByteArray): Pair<ByteArray, ByteArray>
    fun decrypt(iv: ByteArray, sealed: ByteArray): ByteArray
}

/** AES-256-GCM with a key made in, and never leaving, the Android Keystore. */
class KeystoreCipher(private val alias: String = "daytrace_pairing") : TokenCipher {
    override fun encrypt(plain: ByteArray): Pair<ByteArray, ByteArray> {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, key()) }
        return cipher.iv to cipher.doFinal(plain)
    }

    override fun decrypt(iv: ByteArray, sealed: ByteArray): ByteArray =
        Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv)) }.doFinal(sealed)

    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(alias, null) as? SecretKey)?.let { return it }
        val spec = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build()
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).apply { init(spec) }.generateKey()
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}

/** This phone's pairing: how to reach the hub, and which profile and name the hub gave it. */
data class Pairing(val config: HubConfig, val profile: String, val deviceName: String)

/** The pairing without the token. */
data class PairingInfo(val baseUrl: String, val deviceId: String, val profile: String)

/**
 * The current pairing, and (DT-22) the pairings set aside: forgotten ones ("Forget this hub") and ones a newer pairing
 * replaced. A set-aside pairing is never used to sync; it is kept only so that pairing with that hub again proves
 * this is the same phone, which then keeps its first id instead of getting a new one. Its token stays encrypted
 * like the current one, and it is sent only to a hub that first proved it holds that token's hash.
 */
class PairingStore(context: Context, private val cipher: TokenCipher = KeystoreCipher()) : HubConfigSource {
    private val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)

    /** Where and as what this phone is paired, without touching the token (the status screen asks every few seconds). */
    fun info(): PairingInfo? {
        if (!prefs.contains(KEY_TOKEN)) return null
        val url = prefs.getString(KEY_URL, null) ?: return null
        val deviceId = prefs.getString(KEY_DEVICE, null) ?: return null
        return PairingInfo(url, deviceId, prefs.getString(KEY_PROFILE, null).orEmpty())
    }

    override fun load(): HubConfig? = pairing()?.config

    /**
     * The saved pairing, or null when there is none. A token that cannot be decrypted (the Keystore key is gone,
     * which happens only if Android wipes it) can never be used again, so the pairing is cleared: the status screen
     * then offers pairing again instead of looking paired while nothing syncs.
     */
    fun pairing(): Pairing? {
        val url = prefs.getString(KEY_URL, null) ?: return null
        val deviceId = prefs.getString(KEY_DEVICE, null) ?: return null
        val sealed = prefs.getString(KEY_TOKEN, null) ?: return null
        val iv = prefs.getString(KEY_IV, null) ?: return null
        val token = runCatching { String(cipher.decrypt(decode(iv), decode(sealed)), Charsets.UTF_8) }.getOrNull()
            ?: return null.also { discardCurrent() }
        return Pairing(HubConfig(url, token, deviceId), prefs.getString(KEY_PROFILE, null).orEmpty(), prefs.getString(KEY_NAME, null).orEmpty())
    }

    /** The hub proved itself at a new address (see [Syncer]): keep using that one. */
    override fun moved(baseUrl: String) {
        if (prefs.contains(KEY_TOKEN)) prefs.edit(commit = true) { putString(KEY_URL, baseUrl) }
    }

    /**
     * Replaces the current pairing, which is set aside (see the class): a pairing of this same device on the same
     * profile replaces its older set-aside copy, whose token no longer works. Written to disk before this returns.
     */
    fun save(pairing: Pairing) {
        val (iv, sealed) = cipher.encrypt(pairing.config.token.toByteArray(Charsets.UTF_8))
        val kept = (listOfNotNull(currentEntry()) + formerEntries())
            .filterNot { it.optString("device") == pairing.config.deviceId && it.optString("profile") == pairing.profile }
        prefs.edit(commit = true) {
            putString(KEY_URL, pairing.config.baseUrl)
            putString(KEY_DEVICE, pairing.config.deviceId)
            putString(KEY_PROFILE, pairing.profile)
            putString(KEY_NAME, pairing.deviceName)
            putString(KEY_IV, encode(iv))
            putString(KEY_TOKEN, encode(sealed))
            putString(KEY_FORMER, JSONArray(kept.take(MAX_FORMER)).toString())
        }
    }

    /** Forgets the hub: this phone stops sending to it. The pairing is set aside, so pairing with it again keeps this id. */
    fun clear() {
        val kept = listOfNotNull(currentEntry()) + formerEntries()
        prefs.edit(commit = true) {
            CURRENT_KEYS.forEach(::remove)
            putString(KEY_FORMER, JSONArray(kept.take(MAX_FORMER)).toString())
        }
    }

    /** Drops the current pairing without setting it aside: its token can never be used again (lost, or replaced). */
    fun discardCurrent() = prefs.edit(commit = true) { CURRENT_KEYS.forEach(::remove) }

    /**
     * The pairings to offer when pairing again (DT-22): the current one first (even revoked), then the ones set
     * aside, newest first. One whose token can't be decrypted any more is left out.
     */
    fun candidates(): List<HubConfig> = listOfNotNull(pairing()?.config) + formerEntries().mapNotNull { entry ->
        runCatching {
            val token = String(cipher.decrypt(decode(entry.getString("iv")), decode(entry.getString("sealed"))), Charsets.UTF_8)
            HubConfig(entry.getString("url"), token, entry.getString("device"))
        }.getOrNull()
    }

    private fun currentEntry(): JSONObject? {
        val url = prefs.getString(KEY_URL, null) ?: return null
        val device = prefs.getString(KEY_DEVICE, null) ?: return null
        val iv = prefs.getString(KEY_IV, null) ?: return null
        val sealed = prefs.getString(KEY_TOKEN, null) ?: return null
        return JSONObject().put("url", url).put("device", device).put("profile", prefs.getString(KEY_PROFILE, null).orEmpty())
            .put("iv", iv).put("sealed", sealed)
    }

    private fun formerEntries(): List<JSONObject> = runCatching {
        val array = JSONArray(prefs.getString(KEY_FORMER, null) ?: "[]")
        (0 until array.length()).map(array::getJSONObject)
    }.getOrDefault(emptyList())

    companion object {
        private const val KEY_URL = "base_url"
        private const val KEY_DEVICE = "device_id"
        private const val KEY_PROFILE = "profile"
        private const val KEY_NAME = "device_name"
        private const val KEY_IV = "token_iv"
        private const val KEY_TOKEN = "token_sealed"
        private const val KEY_FORMER = "former" // DT-22: pairings set aside, newest first
        private val CURRENT_KEYS = listOf(KEY_URL, KEY_DEVICE, KEY_PROFILE, KEY_NAME, KEY_IV, KEY_TOKEN)
        const val MAX_FORMER = 5

        private fun encode(bytes: ByteArray) = Base64.encodeToString(bytes, Base64.NO_WRAP)
        private fun decode(text: String) = Base64.decode(text, Base64.NO_WRAP)

        @Volatile private var instance: PairingStore? = null

        fun get(context: Context): PairingStore = instance ?: synchronized(this) {
            instance ?: PairingStore(context.applicationContext).also { instance = it }
        }
    }
}
