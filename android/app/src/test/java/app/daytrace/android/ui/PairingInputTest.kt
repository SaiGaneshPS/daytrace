// DT-22: what people type or scan when pairing: codes, hub addresses and the QR code's text.
package app.daytrace.android.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class PairingInputTest {
    @Test
    fun codesAreSixDigitsHoweverTheyAreTyped() {
        for (typed in listOf("493817", " 493 817 ", "493-817")) assertEquals(typed, "493817", PairingInput.code(typed))
        for (typed in listOf("", "49381", "4938170", "49381a", "493_817", "٤٩٣٨١٧")) {
            assertEquals(typed, null, PairingInput.code(typed))
        }
    }

    @Test
    fun addressesGetASchemeAndThePersonalPort() {
        val cases = mapOf(
            "192.168.1.23" to "http://192.168.1.23:8765",
            "192.168.1.23:8766" to "http://192.168.1.23:8766",
            " http://192.168.1.23:8765/ " to "http://192.168.1.23:8765",
            "http://My-PC.local:8767/api/v1/health?x=1" to "http://my-pc.local:8767",
            "http://user:secret@192.168.1.23" to "http://192.168.1.23:8765",
            "[fe80::1]:8765" to "http://[fe80::1]:8765",
            "[fd7a:115c:a1e0::1]" to "http://[fd7a:115c:a1e0::1]:8765",
        )
        for ((typed, expected) in cases) assertEquals(typed, expected, PairingInput.address(typed))
        for (typed in listOf("", "   ", "192.168.1.23 8765", "ftp://192.168.1.23", "http://")) {
            assertEquals(typed, null, PairingInput.address(typed))
        }
    }

    @Test
    fun theHubsQrCodeGivesTheAddressAndTheCode() {
        val text = """{"daytrace":1,"url":"http://192.168.1.23:8765","code":"493817"}"""
        assertEquals(PairingQr("http://192.168.1.23:8765", "493817"), PairingInput.qr(text))
    }

    @Test
    fun otherQrCodesAreNotPairingCodes() {
        val others = listOf(
            "https://example.com",
            """{"daytrace":2,"url":"http://192.168.1.23:8765","code":"493817"}""",
            """{"daytrace":1,"url":"not a url","code":"493817"}""",
            """{"daytrace":1,"url":"http://192.168.1.23:8765","code":"12"}""",
            """{"daytrace":1,"code":"493817"}""",
        )
        for (text in others) assertEquals(text, null, PairingInput.qr(text))
    }
}
