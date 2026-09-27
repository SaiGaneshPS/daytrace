// DT-19: the few icons the app needs that material-icons-core lacks, drawn here instead of pulling in the
// 40 MB extended icon set.
package app.daytrace.android.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.dp

object DaytraceIcons {
    /** Material "bar_chart" (Apache 2.0), for usage access. */
    val BarChart: ImageVector by lazy { icon("BarChart", "M5,9.2h3L8,19L5,19zM10.6,5h2.8v14h-2.8zM16.2,13L19,13v6h-2.8z") }

    /** Material "qr_code_scanner" (Apache 2.0), for pairing with the hub (DT-22). */
    val QrScanner: ImageVector by lazy {
        icon(
            "QrScanner",
            "M9.5,6.5v3h-3v-3H9.5M11,5H5v6h6V5L11,5zM9.5,14.5v3h-3v-3H9.5M11,13H5v6h6V13L11,13zM17.5,6.5v3h-3v-3H17.5" +
                "M19,5h-6v6h6V5L19,5zM13,13h1.5v1.5H13V13zM14.5,14.5H16V16h-1.5V14.5zM16,13h1.5v1.5H16V13zM13,16h1.5v1.5H13V16z" +
                "M14.5,17.5H16V19h-1.5V17.5zM16,16h1.5v1.5H16V16zM17.5,14.5H19V16h-1.5V14.5zM17.5,17.5H19V19h-1.5V17.5z" +
                "M22,7h-2V4h-3V2h5V7zM22,22v-5h-2v3h-3v2H22zM2,22h5v-2H4v-3H2V22zM2,2v5h2V4h3V2H2z",
        )
    }

    private fun icon(name: String, path: String): ImageVector =
        ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f)
            .addPath(PathParser().parsePathString(path).toNodes(), fill = SolidColor(Color.Black))
            .build()
}
