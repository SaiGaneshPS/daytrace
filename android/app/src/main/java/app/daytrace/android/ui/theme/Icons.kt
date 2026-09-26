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

    private fun icon(name: String, path: String): ImageVector =
        ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f)
            .addPath(PathParser().parsePathString(path).toNodes(), fill = SolidColor(Color.Black))
            .build()
}
