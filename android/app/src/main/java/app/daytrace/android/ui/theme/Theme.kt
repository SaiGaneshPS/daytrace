// DT-19: the app theme, light and dark, with the brand gradient used by headers.
package app.daytrace.android.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color

private val LightColors = lightColorScheme(
    primary = Indigo,
    onPrimary = Color.White,
    secondary = Blush,
    onSecondary = Color.White,
    tertiary = Mint,
    onTertiary = Ink,
    background = Paper,
    onBackground = Ink,
    surface = Color.White,
    onSurface = Ink,
    surfaceVariant = Color(0xFFF2ECFF),
    onSurfaceVariant = Color(0xFF514A6B),
)

private val DarkColors = darkColorScheme(
    primary = IndigoLight,
    onPrimary = Color.White,
    secondary = Blush,
    onSecondary = Color.White,
    tertiary = Mint,
    onTertiary = Night,
    background = Night,
    onBackground = Color(0xFFF1EEFF),
    surface = NightSurface,
    onSurface = Color(0xFFF1EEFF),
    surfaceVariant = Color(0xFF2B2645),
    onSurfaceVariant = Color(0xFFC9C2E6),
)

/** Colors Material does not have a slot for: permission states and the header gradient. */
@Immutable
data class DaytraceExtras(val granted: Color, val needed: Color, val headerGradient: Brush)

val LocalDaytraceExtras = staticCompositionLocalOf {
    DaytraceExtras(Granted, Needed, Brush.linearGradient(listOf(Indigo, Blush, Sunrise)))
}

@Composable
fun DaytraceTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val extras = if (darkTheme) {
        DaytraceExtras(GrantedDark, NeededDark, Brush.linearGradient(listOf(IndigoLight, Blush, Sunrise)))
    } else {
        DaytraceExtras(Granted, Needed, Brush.linearGradient(listOf(Indigo, Blush, Sunrise)))
    }
    androidx.compose.runtime.CompositionLocalProvider(LocalDaytraceExtras provides extras) {
        MaterialTheme(
            colorScheme = if (darkTheme) DarkColors else LightColors,
            typography = DaytraceTypography,
            content = content,
        )
    }
}
