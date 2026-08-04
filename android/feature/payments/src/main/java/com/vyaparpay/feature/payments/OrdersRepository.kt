package com.vyaparpay.feature.payments

import com.vyaparpay.core.network.ApiResult

/**
 * The device-orders seam (docs/03-android-architecture.md §2.1 repository
 * pattern) — `GET /v1/orders` (docs/13-api-contracts.md §3.4) has no
 * client-side caller yet and `:app`'s Hilt graph binds no `VyaparApi`, so
 * [SeededOrdersRepository] stands in with the same canonical row docs/13 §3.4
 * serves, exactly like [SeededPaymentRepository] does for the vendor-payment
 * flow. Shared by both `OrdersScreen` (the list) and `OrderTrackingScreen`
 * (one order's detail) — they are the same `device_orders` flow group
 * (docs/03 §3.8) and there is only one data source between them.
 */
public interface OrdersRepository {

    /** Fetches up to [limit] device orders. */
    public suspend fun getOrders(limit: Int): ApiResult<OrdersPage>
}

/** One page of device orders; [totalCount] is the server-reported total, mirroring docs/13 §3.4's `meta.total`. */
public data class OrdersPage(
    public val items: List<DeviceOrder>,
    public val totalCount: Int,
)

/** Mirrors docs/12-data-models.md §3.4's `device_orders.device_type` CHECK constraint. */
public enum class DeviceType {
    SOUNDBOX,
    QR_KIT,
}

/** Mirrors docs/12 §3.4's `device_orders.status` CHECK constraint, one member per allowed value. */
public enum class DeviceOrderStatus {
    PLACED,
    PACKED,
    IN_TRANSIT,
    DELIVERED,
    RETURNED,
}

/** One device order (docs/12 §3.4's `device_orders` DDL). */
public data class DeviceOrder(
    public val orderId: String,
    public val item: String,
    public val deviceType: DeviceType,
    public val status: DeviceOrderStatus,
    public val courier: String?,
    public val trackingId: String?,
    /** ISO `yyyy-MM-dd`; see [Settlement.batchDate]'s kdoc for why this is a plain string, not a date type. */
    public val etaDate: String?,
)

/**
 * Seeds the canonical soundbox order from docs/13 §3.4 verbatim, plus a
 * delivered QR-kit order and a freshly-placed order so the list is
 * non-trivial.
 *
 * **Doc discrepancy, not fixed here.** The order id is **`ord_snd_0712`** —
 * docs/13 §3.4's `GET /v1/orders` response body, verbatim — not the
 * `ord_sb_0712` docs/12 §3.4's DDL comment uses as its illustrative primary
 * key example. The API contract (docs/13) is what a client actually parses
 * off the wire, so it wins; docs/12's comment is illustrative DDL shorthand,
 * not a seeded value.
 */
public class SeededOrdersRepository : OrdersRepository {

    override suspend fun getOrders(limit: Int): ApiResult<OrdersPage> =
        ApiResult.Success(OrdersPage(items = ALL_ORDERS.take(limit), totalCount = ALL_ORDERS.size))

    public companion object {
        /** docs/13 §3.4's canonical row, verbatim. */
        public val CANONICAL_SOUNDBOX: DeviceOrder = DeviceOrder(
            orderId = "ord_snd_0712",
            item = "VyaparPay Soundbox",
            deviceType = DeviceType.SOUNDBOX,
            status = DeviceOrderStatus.IN_TRANSIT,
            courier = "Delhivery",
            trackingId = "DLV88421907",
            etaDate = "2026-07-26",
        )

        private val DELIVERED_QR_KIT = DeviceOrder(
            orderId = "ord_qr_0705",
            item = "VyaparPay QR Kit",
            deviceType = DeviceType.QR_KIT,
            status = DeviceOrderStatus.DELIVERED,
            courier = "Delhivery",
            trackingId = "DLV88400211",
            etaDate = "2026-07-08",
        )

        private val PLACED_SOUNDBOX = DeviceOrder(
            orderId = "ord_snd_0725",
            item = "VyaparPay Soundbox",
            deviceType = DeviceType.SOUNDBOX,
            status = DeviceOrderStatus.PLACED,
            courier = null,
            trackingId = null,
            etaDate = null,
        )

        private val ALL_ORDERS: List<DeviceOrder> = listOf(CANONICAL_SOUNDBOX, DELIVERED_QR_KIT, PLACED_SOUNDBOX)
    }
}
