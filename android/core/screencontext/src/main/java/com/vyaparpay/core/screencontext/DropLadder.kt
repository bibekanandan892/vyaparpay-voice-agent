package com.vyaparpay.core.screencontext

import kotlin.math.ceil
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * docs/07-ui-semantic-context.md §7's token-budget drop ladder and the
 * `chars / 3.5` estimate proxy — the client-side half of rule 8 ("serialize
 * and hand off"). Operates on the [ScreenContextIr.toJson] `JsonObject`
 * shape directly, not the typed [ScreenComponent] hierarchy, deliberately
 * mirroring `backend/app/context/context_compressor.py`'s own dict-shaped
 * `_rung*` functions almost line for line — same six rungs, same constants,
 * same order of operations, same "keep re-estimating after each rung, stop
 * as soon as it fits" loop.
 *
 * **Known cross-language duplication risk, called out explicitly (matching
 * how `ctx_delta.v1.json`'s own description flags its duplicated role
 * enum):** this ladder and `ContextCompressor.recompress_oversize` are two
 * independent implementations of the same docs/07 §7 spec, one in Kotlin, one
 * in Python, with no shared source of truth — protocol/ does not (yet) carry
 * a machine-readable constant for the rung thresholds either
 * (`token_estimate.py`'s own kdoc makes the identical observation about the
 * `chars/3.5` proxy). A future change to a rung threshold on one side and not
 * the other would silently desync client-side drop behavior from the
 * server's defense-in-depth re-verification (docs/08 §4.3) — the two would
 * still each individually honor the ≤300-token budget, just at different
 * points, which is low-severity but real. Mitigated today only by this
 * comment plus [DropLadderTest] mirroring `test_context_compressor.py`'s rung
 * coverage line for line so a human diff between the two test files would
 * catch drift; no automated cross-language check exists.
 */
public object DropLadder {

    // docs/07 §7: "a chars / 3.5 proxy with a 10% safety margin" — identical
    // constants to backend/app/context/token_estimate.py.
    private const val CHARS_PER_TOKEN = 3.5
    private const val SAFETY_MARGIN = 1.10
    public const val SNAPSHOT_TOKEN_BUDGET: Int = 300

    private const val MAX_LIST_ITEMS_PER_LIST = 3 // rung 1
    private const val LABEL_VALUE_MAX_CHARS = 48 // rung 2
    private const val ELLIPSIS = "…"
    private val NAV_CHROME_ROLES = setOf("tab", "toggle", "secondary_cta") // rung 4
    private val MINIMAL_ROLES = setOf("dialog", "error_banner", "snackbar", "amount_field", "recipient", "primary_cta") // rung 5
    private val SCREEN_NAME_ONLY_KEYS = listOf("v", "screen", "flow", "last_api") // rung 6

    /** Over-estimate of [text]'s token count, rounded up so it never understates a budget check. */
    public fun estimateTokens(text: String): Int = ceil((text.length / CHARS_PER_TOKEN) * SAFETY_MARGIN).toInt()

    /** Compact (no extraneous whitespace) rendering, matching orjson's default on the backend. */
    public fun compactJson(ir: JsonObject): String = ir.toString()

    public data class RecompressResult(val ir: JsonObject, val droppedRungs: List<Int>, val estimatedTokens: Int)

    /**
     * Re-applies the docs/07 §7 ladder from rung 1, stopping as soon as the
     * estimate fits [SNAPSHOT_TOKEN_BUDGET] (or rung 6, the shared floor,
     * whichever comes first) — the same contract as
     * `ContextCompressor.recompress_oversize`.
     */
    public fun recompressOversize(ir: JsonObject): RecompressResult {
        var current = ir
        var tokens = estimateTokens(compactJson(current))
        if (tokens <= SNAPSHOT_TOKEN_BUDGET) return RecompressResult(current, emptyList(), tokens)

        val applied = mutableListOf<Int>()
        val rungs = listOf(
            1 to ::rung1CapListItems,
            2 to ::rung2TruncateLabelsAndValues,
            3 to ::rung3DropDisabledNonPrimary,
            4 to ::rung4DropNavChrome,
            5 to ::rung5Minimal,
            6 to ::rung6ScreenNameOnly,
        )
        for ((rungNo, apply) in rungs) {
            current = apply(current)
            applied += rungNo
            tokens = estimateTokens(compactJson(current))
            if (tokens <= SNAPSHOT_TOKEN_BUDGET) break
        }
        return RecompressResult(current, applied, tokens)
    }

    private fun JsonObject.components(): JsonArray = this["components"] as? JsonArray ?: JsonArray(emptyList())

    private fun JsonObject.withComponents(components: List<JsonElement>): JsonObject =
        buildJsonObject {
            this@withComponents.forEach { (k, v) -> put(k, v) }
            put("components", buildJsonArray { components.forEach { add(it) } })
        }

    /** Rung 1: drop `list_item`s beyond the top 3 per list; the `list` entry itself always survives. */
    private fun rung1CapListItems(ir: JsonObject): JsonObject {
        val out = mutableListOf<JsonElement>()
        var inList = false
        var kept = 0
        for (component in ir.components()) {
            val role = component.jsonObject["role"]?.jsonPrimitive?.contentOrNull
            when {
                role == "list" -> {
                    inList = true
                    kept = 0
                    out += component
                }
                role == "list_item" && inList -> {
                    kept += 1
                    if (kept <= MAX_LIST_ITEMS_PER_LIST) out += component
                }
                else -> {
                    inList = false
                    out += component
                }
            }
        }
        return ir.withComponents(out)
    }

    private fun truncate(text: String): String =
        if (text.length <= LABEL_VALUE_MAX_CHARS) text else text.take(LABEL_VALUE_MAX_CHARS - 1) + ELLIPSIS

    /** Rung 2: labels and values truncated to 48 chars with an ellipsis. */
    private fun rung2TruncateLabelsAndValues(ir: JsonObject): JsonObject {
        val out = ir.components().map { component ->
            buildJsonObject {
                component.jsonObject.forEach { (key, value) ->
                    if ((key == "label" || key == "value") && value is JsonPrimitive && value.isString) {
                        put(key, truncate(value.content))
                    } else {
                        put(key, value)
                    }
                }
            }
        }
        return ir.withComponents(out)
    }

    /** Rung 3: disabled, non-`primary_cta` interactive components dropped. */
    private fun rung3DropDisabledNonPrimary(ir: JsonObject): JsonObject {
        val out = ir.components().filterNot { component ->
            val obj = component.jsonObject
            val role = obj["role"]?.jsonPrimitive?.contentOrNull
            val enabled = obj["enabled"]?.jsonPrimitive?.booleanOrNull
            role != "primary_cta" && enabled == false
        }
        return ir.withComponents(out)
    }

    /** Rung 4: navigation chrome (`tab`, `toggle`, `secondary_cta`) dropped outright. */
    private fun rung4DropNavChrome(ir: JsonObject): JsonObject {
        val out = ir.components().filterNot { component ->
            component.jsonObject["role"]?.jsonPrimitive?.contentOrNull in NAV_CHROME_ROLES
        }
        return ir.withComponents(out)
    }

    /** Rung 5: only the six interruption/money-fact roles survive. */
    private fun rung5Minimal(ir: JsonObject): JsonObject {
        val out = ir.components().filter { component ->
            component.jsonObject["role"]?.jsonPrimitive?.contentOrNull in MINIMAL_ROLES
        }
        return ir.withComponents(out)
    }

    /**
     * Rung 6: the shared docs/07 §8 floor. Mirrors
     * `ContextCompressor._rung6_screen_name_only`'s own judgment call
     * (documented there): the doc's summary table lists this rung's contents
     * as literally `{v, screen, flow, last_api}`, but the schema's `required`
     * list demands all eight top-level keys unconditionally — so the other
     * four reset to their empty-equivalents (`components: []`,
     * `last_action: null`, `dirty_fields: []`, `loading: false`) rather than
     * being dropped, keeping the floor a structurally valid
     * `screen_context/v1` document a later delta can still merge onto.
     */
    private fun rung6ScreenNameOnly(ir: JsonObject): JsonObject = buildJsonObject {
        SCREEN_NAME_ONLY_KEYS.forEach { key -> ir[key]?.let { put(key, it) } }
        put("components", buildJsonArray {})
        put("last_action", kotlinx.serialization.json.JsonNull)
        put("dirty_fields", buildJsonArray {})
        put("loading", false)
    }
}
