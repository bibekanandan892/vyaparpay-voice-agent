package com.vyaparpay

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.vyaparpay.core.ui.theme.VyaparTheme
import com.vyaparpay.navigation.AppNavHost
import dagger.hilt.android.AndroidEntryPoint

/**
 * The app's single activity. Everything below it is Compose.
 *
 * One activity is what makes the context pipeline tractable: `UiTreeCollector`
 * tracks window attach/detach against one host rather than reconciling a stack
 * of activities, and `NavigationTracker` binds once to one `NavController`.
 */
@AndroidEntryPoint
public class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            VyaparTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    AppNavHost()
                }
            }
        }
    }
}
