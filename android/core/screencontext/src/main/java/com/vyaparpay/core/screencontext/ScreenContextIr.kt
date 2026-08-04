package com.vyaparpay.core.screencontext

import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/**
 * The `screen_context/v1` component vocabulary (docs/07-ui-semantic-context.md
 * §4), field-for-field against `protocol/schemas/screen_context.v1.json`'s
 * per-role `$defs`.
 *
 * Modeled as a sealed hierarchy, not one data class with nullable per-role
 * fields — the same choice `AppEvent` (`:core:analytics`) already makes and
 * documents: the wire schema is itself a `oneOf` keyed on `role`, each
 * variant has its own required/optional fields (a `dialog` has no `enabled`;
 * a `primary_cta` has no `visible`), and a sealed `when` over this type is
 * exhaustive at compile time instead of trusting sixteen call sites to leave
 * the right fields null.
 *
 * **Field-presence policy** (docs/07 §4 header note, carried into [toJson]):
 * `enabled`/`visible` at their default are omitted to save tokens, EXCEPT
 * `primary_cta`/`secondary_cta`'s `enabled` (always carried — the headline
 * fact for a CTA) and `dialog`/`snackbar`/`error_banner`/`alert_banner`'s
 * `visible` (always carried — visibility is the headline fact for a
 * transient role). `focused` is carried only when `true`.
 */
public sealed interface ScreenComponent {
    public val role: ScreenComponentRole
    public val label: String

    public data class AmountField(
        override val label: String,
        val value: String,
        val focused: Boolean = false,
    ) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.AMOUNT_FIELD
    }

    public data class Recipient(override val label: String, val value: String) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.RECIPIENT
    }

    public data class PrimaryCta(override val label: String, val enabled: Boolean) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.PRIMARY_CTA
    }

    public data class SecondaryCta(override val label: String, val enabled: Boolean) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.SECONDARY_CTA
    }

    public data class TextField(
        override val label: String,
        val value: String,
        val focused: Boolean = false,
    ) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.TEXT_FIELD
    }

    public data class ListComponent(
        override val label: String,
        val visibleCount: Int,
        val totalCount: Int,
    ) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.LIST
    }

    public data class ListItem(override val label: String, val value: String) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.LIST_ITEM
    }

    public data class Dialog(override val label: String, val visible: Boolean = true) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.DIALOG
    }

    public data class Snackbar(override val label: String, val visible: Boolean = true) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.SNACKBAR
    }

    public data class Tab(override val label: String, val selected: Boolean) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.TAB
    }

    public data class Toggle(
        override val label: String,
        val on: Boolean,
        val enabled: Boolean = true,
    ) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.TOGGLE
    }

    public data class BalanceDisplay(override val label: String, val value: String) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.BALANCE_DISPLAY
    }

    public data class StatusBadge(override val label: String, val value: String) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.STATUS_BADGE
    }

    public data class ErrorBanner(override val label: String, val visible: Boolean = true) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.ERROR_BANNER
    }

    public data class AlertBanner(override val label: String, val visible: Boolean = true) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.ALERT_BANNER
    }

    public data class Image(override val label: String) : ScreenComponent {
        override val role: ScreenComponentRole get() = ScreenComponentRole.IMAGE
    }
}

/** docs/07 §5 rule 7 / §6 shape — `{"type": "tap", "target": "Pay Now", "ts": ...}`. */
public data class LastAction(val type: String, val target: String, val ts: Long)

/** docs/07 §5 rule 7 shape — the most recent non-2xx response for this screen. */
public data class LastApi(val method: String, val path: String, val status: Int, val errorCode: String)

/**
 * The `screen_context/v1` IR (docs/07 §3) — this is the type a future,
 * not-yet-dispatched task (`ScreenContextPublisher`/`AppStateManager`, docs/03
 * §3.10-§3.11) consumes; this task defines its shape but does not wire it to
 * `ContextChannel` itself.
 */
public data class ScreenContextIr(
    val screen: String,
    val flow: String,
    val components: List<ScreenComponent>,
    val lastAction: LastAction?,
    val lastApi: LastApi?,
    val dirtyFields: List<String>,
    val loading: Boolean,
) {
    /** The one fixed value docs/13 §1.1 uses to reject an unknown IR version. */
    public val v: String get() = "screen_context/v1"
}

/**
 * docs/07 §5 rule 8 (serialize), the first half: a deterministic, hand-built
 * `JsonObject` rather than derived (`@Serializable`) encoding. Hand-building
 * gives exact control over key order and per-role field presence — both load-
 * bearing here, since this is the same shape the golden test in
 * `SemanticSnapshotBuilderTest` compares field-for-field against
 * `protocol/fixtures/context/screen_context_payment_decline.json`, and the
 * one [DropLadder] operates on directly (mirroring the backend
 * `ContextCompressor`'s own dict-shaped rungs — see that file's kdoc for why
 * that mirroring matters).
 */
public fun ScreenContextIr.toJson(): JsonObject = buildJsonObject {
    put("v", v)
    put("screen", screen)
    put("flow", flow)
    putJsonArray("components") { components.forEach { add(it.toJson()) } }
    put("last_action", lastAction?.toJson() ?: JsonNull)
    put("last_api", lastApi?.toJson() ?: JsonNull)
    putJsonArray("dirty_fields") { dirtyFields.forEach { add(it) } }
    put("loading", loading)
}

private fun LastAction.toJson(): JsonObject = buildJsonObject {
    put("type", type)
    put("target", target)
    put("ts", ts)
}

private fun LastApi.toJson(): JsonObject = buildJsonObject {
    put("method", method)
    put("path", path)
    put("status", status)
    put("error_code", errorCode)
}

internal fun ScreenComponent.toJson(): JsonObject = buildJsonObject {
    put("role", role.wireName)
    put("label", label)
    when (val self = this@toJson) {
        is ScreenComponent.AmountField -> {
            put("value", self.value)
            if (self.focused) put("focused", true)
        }
        is ScreenComponent.Recipient -> put("value", self.value)
        is ScreenComponent.PrimaryCta -> put("enabled", self.enabled)
        is ScreenComponent.SecondaryCta -> put("enabled", self.enabled)
        is ScreenComponent.TextField -> {
            put("value", self.value)
            if (self.focused) put("focused", true)
        }
        is ScreenComponent.ListComponent -> {
            put("visible_count", self.visibleCount)
            put("total_count", self.totalCount)
        }
        is ScreenComponent.ListItem -> put("value", self.value)
        is ScreenComponent.Dialog -> put("visible", self.visible)
        is ScreenComponent.Snackbar -> put("visible", self.visible)
        is ScreenComponent.Tab -> put("selected", self.selected)
        is ScreenComponent.Toggle -> {
            put("value", if (self.on) "on" else "off")
            if (!self.enabled) put("enabled", false)
        }
        is ScreenComponent.BalanceDisplay -> put("value", self.value)
        is ScreenComponent.StatusBadge -> put("value", self.value)
        is ScreenComponent.ErrorBanner -> put("visible", self.visible)
        is ScreenComponent.AlertBanner -> put("visible", self.visible)
        is ScreenComponent.Image -> Unit
    }
}
