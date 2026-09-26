// DT-21 / DT-22: where the hub address, the device id and the token are kept. DT-22 adds pairing (discovery, QR
// code) and stores the token encrypted with a key from the Android Keystore. Until then the phone is not paired:
// events wait safely in the database and nothing is sent.
package app.daytrace.android.sync

object PairingStore : HubConfigSource {
    override fun load(): HubConfig? = null
}
