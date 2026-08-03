package com.vyaparpay.core.ui.theme

import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.ui.graphics.Color

private val BrandGreen = Color(0xFF0B6E4F)
private val BrandGreenLight = Color(0xFF63D2A8)
private val BrandSaffron = Color(0xFFF4A23C)
private val SurfaceLight = Color(0xFFFCFCF9)
private val SurfaceDark = Color(0xFF12151A)
private val OnBrand = Color(0xFFFFFFFF)
private val OnSurfaceLight = Color(0xFF1A1C1E)
private val OnSurfaceDark = Color(0xFFE3E2E6)
private val ErrorRed = Color(0xFFB3261E)

internal val VyaparLightColors = lightColorScheme(
    primary = BrandGreen,
    onPrimary = OnBrand,
    secondary = BrandSaffron,
    onSecondary = OnSurfaceLight,
    background = SurfaceLight,
    onBackground = OnSurfaceLight,
    surface = SurfaceLight,
    onSurface = OnSurfaceLight,
    error = ErrorRed,
    onError = OnBrand,
)

internal val VyaparDarkColors = darkColorScheme(
    primary = BrandGreenLight,
    onPrimary = OnSurfaceDark,
    secondary = BrandSaffron,
    onSecondary = OnSurfaceDark,
    background = SurfaceDark,
    onBackground = OnSurfaceDark,
    surface = SurfaceDark,
    onSurface = OnSurfaceDark,
    error = ErrorRed,
    onError = OnBrand,
)
