// DT-58: the window theme is light or dark as the phone is, so the Dashboard tab's WebView (whose
// prefers-color-scheme follows the theme's isLightTheme) shows the hub's pages in the same mode as the app around them.
package app.daytrace.android.ui

import android.app.Application
import android.view.ContextThemeWrapper
import androidx.test.core.app.ApplicationProvider
import app.daytrace.android.R
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WindowThemeTest {
    private fun isLight(): Boolean {
        val themed = ContextThemeWrapper(ApplicationProvider.getApplicationContext<Application>(), R.style.Theme_Daytrace)
        val values = themed.obtainStyledAttributes(intArrayOf(android.R.attr.isLightTheme))
        return try {
            values.getBoolean(0, true)
        } finally {
            values.recycle()
        }
    }

    @Test
    fun lightByDay() = assertTrue(isLight())

    @Test
    @Config(qualifiers = "night")
    fun darkAtNight() = assertFalse(isLight())
}
