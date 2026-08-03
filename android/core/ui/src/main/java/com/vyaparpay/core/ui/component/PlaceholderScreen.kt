package com.vyaparpay.core.ui.component

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag

/**
 * The stand-in every feature route renders until its real screen lands.
 *
 * It carries a testTag from day one so the navigation graph is already
 * assertable by Compose UI tests, and so the first capture run over the app
 * produces named nodes rather than an anonymous tree.
 */
@Composable
public fun PlaceholderScreen(
    title: String,
    screenTestTag: String,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            .testTag(screenTestTag),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = title,
            style = MaterialTheme.typography.headlineSmall,
        )
    }
}
