// DT-21 / DT-22: where the hub address, the device id and the token are kept. The token is encrypted with an AES
// key that lives in the Android Keystore (it cannot be read out of the phone), and the app's data is never backed
// up (AndroidManifest.xml), so the token stays on this phone.
package app.daytrace.android.sync

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.content.edit
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
     * The saved pairing, or null when there is none or the token cannot be decrypted (the Keystore key is gone,
     * which happens only if Android wipes it): then the phone simply pairs again.
     */
    fun pairing(): Pairing? {
        val url = prefs.getString(KEY_URL, null) ?: return null
        val deviceId = prefs.getString(KEY_DEVICE, null) ?: return null
        val sealed = prefs.getString(KEY_TOKEN, null) ?: return null
        val iv = prefs.getString(KEY_IV, null) ?: return null
        val token = runCatching { String(cipher.decrypt(decode(iv), decode(sealed)), Charsets.UTF_8) }.getOrNull() ?: return null
        return Pairing(HubConfig(url, token, deviceId), prefs.getString(KEY_PROFILE, null).orEmpty(), prefs.getString(KEY_NAME, null).orEmpty())
    }

    /** Replaces any earlier pairing. Written to disk before this returns. */
    fun save(pairing: Pairing) {
        val (iv, sealed) = cipher.encrypt(pairing.config.token.toByteArray(Charsets.UTF_8))
        prefs.edit(commit = true) {
            clear()
            putString(KEY_URL, pairing.config.baseUrl)
            putString(KEY_DEVICE, pairing.config.deviceId)
            putString(KEY_PROFILE, pairing.profile)
            putString(KEY_NAME, pairing.deviceName)
            putString(KEY_IV, encode(iv))
            putString(KEY_TOKEN, encode(sealed))
        }
    }

    /** Forgets the hub. Events stay on the phone and go to whichever hub it pairs with next. */
    fun clear() = prefs.edit(commit = true) { clear() }

    companion object {
        private const val KEY_URL = "base_url"
        private const val KEY_DEVICE = "device_id"
        private const val KEY_PROFILE = "profile"
        private const val KEY_NAME = "device_name"
        private const val KEY_IV = "token_iv"
        private const val KEY_TOKEN = "token_sealed"

        private fun encode(bytes: ByteArray) = Base64.encodeToString(bytes, Base64.NO_WRAP)
        private fun decode(text: String) = Base64.decode(text, Base64.NO_WRAP)

        @Volatile private var instance: PairingStore? = null

        fun get(context: Context): PairingStore = instance ?: synchronized(this) {
            instance ?: PairingStore(context.applicationContext).also { instance = it }
        }
    }
}
