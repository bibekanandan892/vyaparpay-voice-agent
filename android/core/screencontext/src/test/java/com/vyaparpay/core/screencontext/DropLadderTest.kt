package com.vyaparpay.core.screencontext

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * docs/07 §7's drop ladder — mirrors `backend/tests/context/test_context_compressor.py`'s
 * rung coverage (see [DropLadder]'s own kdoc on why this mirroring matters:
 * this is the client-side half of the same spec, in a different language,
 * with no shared source of truth).
 */
class DropLadderTest {

    private fun ir(components: List<String>): JsonObject {
        val json = """
            {"v":"screen_context/v1","screen":"S","flow":"f",
             "components":[${components.joinToString(",")}],
             "last_action":null,"last_api":null,"dirty_fields":[],"loading":false}
        """.trimIndent()
        return Json.parseToJsonElement(json).jsonObject
    }

    private fun component(role: String, label: String, extra: String = ""): String =
        """{"role":"$role","label":"$label" $extra}"""

    @Test
    fun `an already within-budget IR is returned unchanged`() {
        val small = ir(listOf(component("primary_cta", "Pay Now", ""","enabled":true""")))

        val result = DropLadder.recompressOversize(small)

        assertEquals(emptyList<Int>(), result.droppedRungs)
        assertEquals(small, result.ir)
    }

    @Test
    fun `rung 1 alone can bring a list-heavy IR under budget`() {
        val listComponents = listOf(component("list", "Batches", ""","visible_count":30,"total_count":47""")) +
            (1..30).map { component("list_item", "Batch $it", ""","value":"₹1,000"""") }
        val dense = ir(listComponents)

        val result = DropLadder.recompressOversize(dense)

        assertEquals(listOf(1), result.droppedRungs)
        val components = result.ir["components"]!!.jsonArray
        val listItems = components.filter { it.jsonObject["role"]?.jsonPrimitive?.content == "list_item" }
        assertEquals(3, listItems.size)
        val list = components.first { it.jsonObject["role"]?.jsonPrimitive?.content == "list" }
        assertEquals(47, list.jsonObject["total_count"]?.jsonPrimitive?.content?.toInt())
        assertTrue(result.estimatedTokens <= DropLadder.SNAPSHOT_TOKEN_BUDGET)
    }

    @Test
    fun `rung 2 truncates labels and values to 48 chars with an ellipsis`() {
        // `recipient` is a rung-5 minimal-role survivor, so this test proves
        // truncation happened without the result being wiped by a later
        // rung's role filter (unlike `status_badge`, which rung 5 would drop
        // entirely, leaving nothing to assert on). Six components, each with
        // a 100-char label, is over budget untruncated but comfortably under
        // it once rung 2 alone shortens every label to 48 chars — so the
        // ladder stops at rung 2 and every component survives.
        val longLabel = "L".repeat(100)
        val components = (1..6).map { component("recipient", longLabel, ""","value":"Amazon Business"""") }
        val dense = ir(components)

        val result = DropLadder.recompressOversize(dense)

        assertTrue(2 in result.droppedRungs)
        val firstLabel = result.ir["components"]!!.jsonArray.first().jsonObject["label"]!!.jsonPrimitive.content
        assertTrue(firstLabel.length <= 48)
        assertTrue(firstLabel.endsWith("…"))
    }

    @Test
    fun `rung 3 drops disabled secondary_cta but keeps disabled primary_cta`() {
        val components = listOf(component("primary_cta", "Pay Now", ""","enabled":false""")) +
            (1..40).map { component("secondary_cta", "Option $it", ""","enabled":false""") }
        val dense = ir(components)

        val result = DropLadder.recompressOversize(dense)

        assertEquals(listOf(1, 2, 3), result.droppedRungs)
        val roles = result.ir["components"]!!.jsonArray.map { it.jsonObject["role"]!!.jsonPrimitive.content }
        assertEquals(listOf("primary_cta"), roles)
    }

    @Test
    fun `rung 4 drops navigation chrome outright`() {
        val components = (1..40).map { component("tab", "Tab $it", ""","selected":false""") } +
            (1..5).map { component("toggle", "Toggle $it", ""","value":"off"""") }
        val dense = ir(components)

        val result = DropLadder.recompressOversize(dense)

        assertTrue(4 in result.droppedRungs)
        val roles = result.ir["components"]!!.jsonArray.map { it.jsonObject["role"]!!.jsonPrimitive.content }.toSet()
        assertTrue("tab" !in roles && "toggle" !in roles)
    }

    @Test
    fun `rung 5 keeps only the minimal interruption and money-fact roles`() {
        val long = "9".repeat(60)
        val components = listOf(
            component("dialog", "D", ""","visible":true"""),
            component("recipient", "To", ""","value":"$long""""),
        ) + (1..40).map { component("status_badge", "Badge $it", ""","value":"$long"""") }
        val dense = ir(components)

        val result = DropLadder.recompressOversize(dense)

        // 40 long status_badge entries have no list to cap (rung 1), are not
        // shortened enough by a 48-char truncation alone (rung 2), are not
        // disabled (rung 3), and are not nav chrome (rung 4) — only rung 5's
        // role-allowlist actually removes them, so this genuinely exercises
        // rung 5 rather than passing vacuously.
        assertTrue(5 in result.droppedRungs)
        val roles = result.ir["components"]!!.jsonArray.map { it.jsonObject["role"]!!.jsonPrimitive.content }.toSet()
        assertEquals(setOf("dialog", "recipient"), roles)
    }

    @Test
    fun `a genuinely pathological IR bottoms out at the rung 6 screen-name-only floor`() {
        val longValue = "₹" + "9".repeat(100)
        val components = (1..40).map { component("amount_field", "Line item $it", ""","value":"$longValue"""") }
        val pathological = Json.parseToJsonElement(
            """
            {"v":"screen_context/v1","screen":"SettlementsScreen","flow":"settlements",
             "components":[${components.joinToString(",")}],
             "last_action":{"type":"tap","target":"x","ts":1},
             "last_api":{"method":"GET","path":"/settlements","status":200,"error_code":""},
             "dirty_fields":["amount"],"loading":true}
            """.trimIndent(),
        ).jsonObject

        val result = DropLadder.recompressOversize(pathological)

        assertEquals(listOf(1, 2, 3, 4, 5, 6), result.droppedRungs)
        assertEquals(
            setOf("v", "screen", "flow", "last_api", "components", "last_action", "dirty_fields", "loading"),
            result.ir.keys,
        )
        assertEquals("SettlementsScreen", result.ir["screen"]!!.jsonPrimitive.content)
        assertEquals(0, result.ir["components"]!!.jsonArray.size)
        assertTrue(result.estimatedTokens <= DropLadder.SNAPSHOT_TOKEN_BUDGET)
    }

    @Test
    fun `token estimate matches the chars over 3_5 times 1_1 proxy, rounded up`() {
        val text = "x".repeat(35) // 35 / 3.5 = 10, * 1.10 = 11
        assertEquals(11, DropLadder.estimateTokens(text))
    }
}
