package net.skyprobe.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val SkyBlue = Color(0xFF6AA8FF)
val Cyan = Color(0xFF5CD0FF)
val Amber = Color(0xFFFFA94D)
val Danger = Color(0xFFFF5A5F)
val Warn = Color(0xFFFFD23F)

private val Dark = darkColorScheme(
    primary = SkyBlue,
    secondary = Cyan,
    background = Color(0xFF0B0F17),
    surface = Color(0xFF121826),
    surfaceVariant = Color(0xFF182033),
)

private val Light = lightColorScheme(primary = Color(0xFF1D5FD1), secondary = Color(0xFF0E7490))

@Composable
fun SkyProbeTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) =
    MaterialTheme(colorScheme = if (dark) Dark else Light, content = content)
