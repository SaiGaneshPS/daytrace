// DT-22: turning what discovery resolves into a hub address the sync will accept, and readable hub names.
package app.daytrace.android.sync

import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.InetAddress

class HubDiscoveryTest {
    private fun ip(text: String) = InetAddress.getByName(text)

    @Test
    fun aPrivateIpv4AddressIsPicked() {
        assertEquals("http://192.168.1.23:8765", HubAddresses.bestUrl(listOf(ip("fe80::1"), ip("192.168.1.23")), 8765))
        assertEquals("http://100.101.102.103:8766", HubAddresses.bestUrl(listOf(ip("100.101.102.103")), 8766))
    }

    @Test
    fun nothingTheSyncWouldRefuseIsOffered() {
        assertEquals(null, HubAddresses.bestUrl(listOf(ip("8.8.8.8")), 8765)) // public
        assertEquals(null, HubAddresses.bestUrl(listOf(ip("127.0.0.1")), 8765)) // the phone itself
        assertEquals(null, HubAddresses.bestUrl(listOf(ip("fd7a:115c:a1e0::1")), 8765)) // the hub listens on IPv4 only
        assertEquals(null, HubAddresses.bestUrl(emptyList(), 8765))
    }

    @Test
    fun escapedServiceNamesAreMadeReadable() {
        assertEquals("Daytrace hub (personal)", HubAddresses.unescape("Daytrace\\032hub\\032(personal)"))
        assertEquals("Daytrace hub (personal)", HubAddresses.unescape("Daytrace hub (personal)"))
        assertEquals("a.b", HubAddresses.unescape("a\\.b"))
        val laptop = String(Character.toChars(0x1F4BB))
        assertEquals("Sai's $laptop", HubAddresses.unescape("Sai's $laptop"))
        assertEquals("café", HubAddresses.unescape("caf\\195\\169")) // UTF-8 bytes written as \DDD
    }
}
