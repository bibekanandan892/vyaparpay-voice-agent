package com.vyaparpay.core.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable

/**
 * The single Material 3 theme every VyaparPay screen is wrapped in.
 *
 * Dynamic colour is deliberately not wired: the agent reasons about screens the
 * merchant describes ("the orange alert at the top"), and a palette that changes
 * with the user's wallpaper would make that description unreliable.
 */
@Composable
public fun VyaparTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) VyaparDarkColors else VyaparLightColors,
        content = content,
    )
}
