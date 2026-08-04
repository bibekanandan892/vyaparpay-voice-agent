package com.vyaparpay.core.screencontext

/**
 * The framework-adjacent, but framework-INDEPENDENT, walked-tree representation
 * `UiTreeCollector` produces and `SemanticSnapshotBuilder` consumes
 * (docs/07-ui-semantic-context.md §2.1, §5).
 *
 * This is deliberately NOT `androidx.compose.ui.semantics.SemanticsNode`
 * verbatim: a `SemanticsNode` is only valid for the lifetime of one
 * measure/layout pass and cannot be held, compared, or constructed off the UI
 * thread, none of which this file's whole reason for existing (a framework-
 * free, JVM-testable [SemanticSnapshotBuilder]) can tolerate. Instead this is
 * a plain, immutable snapshot of exactly the properties docs/07 §4's role
 * table branches on, walked once on the UI thread by `UiTreeCollector` and
 * then handed downstream as ordinary data.
 *
 * **No bounds, anywhere in this file — on purpose.** docs/07 §3 ("Values over
 * structure") explicitly rejects carrying geometry into the IR: bounds are
 * ~40% of the raw tree's serialized bulk for zero agent value, since the
 * agent speaks, it does not tap. `UiTreeCollector` still *reads* bounds
 * off the real `SemanticsNode` (`boundsInRoot`, size) to compute [visible]/
 * [zeroSize] below — geometry is consumed at the collection boundary and
 * reduced to booleans; it never crosses into this type.
 */

/**
 * The handful of `androidx.compose.ui.semantics.Role` values docs/07 §4's
 * table actually branches on (`Role.Tab` in rule 3 tier (b); `Role.Button` in
 * the `primary_cta`/`image` heuristic tier). Mirrors the real Compose `Role`
 * value class's nine constants one-for-one rather than importing it directly,
 * so every file downstream of [RawSemanticsNode] — in particular
 * [SemanticSnapshotBuilder] and everything it composes with — stays free of
 * an `androidx.compose.ui` import and is testable on a plain JVM.
 */
public enum class RawRole {
    BUTTON,
    CHECKBOX,
    SWITCH,
    RADIO_BUTTON,
    TAB,
    IMAGE,
    DROPDOWN_LIST,
    VALUE_PICKER,
    CAROUSEL,
}

/** Mirrors `androidx.compose.ui.semantics.LiveRegionMode`'s two values. */
public enum class RawLiveRegion { POLITE, ASSERTIVE }

/** Mirrors `androidx.compose.ui.state.ToggleableState`'s three values. */
public enum class RawToggleableState { ON, OFF, INDETERMINATE }

/**
 * Mirrors `androidx.compose.ui.semantics.CollectionInfo`, reduced to the one
 * fact the IR's `list` role needs (docs/07 §4 `list_component`): a total item
 * count. Real `CollectionInfo` carries `rowCount`/`columnCount` separately
 * (a grid concept); `UiTreeCollector` folds them to `max(rowCount, columnCount)`
 * for the common one-dimensional `LazyColumn`/`LazyRow` case (one of the two
 * is always `1` there) — documented in `UiTreeCollector`'s own kdoc as a
 * judgment call, since Compose does not expose a single "item count" semantics
 * property directly.
 */
public data class RawCollectionInfo(val totalCount: Int)

/**
 * One node of the walked, unmerged Compose semantics tree (docs/07 §2.1: read
 * from `SemanticsOwner.unmergedRootSemanticsNode`, never the merged/
 * accessibility-facing tree).
 *
 * Field-by-field mapping to the real `SemanticsNode`/`SemanticsConfiguration`
 * properties this type stands in for (see `UiTreeCollector.toRawNode` for the
 * actual reads): [testTag] <- `SemanticsProperties.TestTag`; [textRuns] <-
 * `SemanticsProperties.Text` (static `AnnotatedString`s, `.text` only);
 * [contentDescription] <- `SemanticsProperties.ContentDescription`;
 * [editableText] <- `SemanticsProperties.EditableText`; [role] <-
 * `SemanticsProperties.Role`; [isDialog] <- presence of
 * `SemanticsProperties.IsDialog`; [paneTitle] <- `SemanticsProperties.PaneTitle`;
 * [liveRegion] <- `SemanticsProperties.LiveRegion`; [collectionInfo] <-
 * `SemanticsProperties.CollectionInfo`; [isListItem] <- presence of
 * `SemanticsProperties.CollectionItemInfo`; [toggleableState] <-
 * `SemanticsProperties.ToggleableState`; [focused] <-
 * `SemanticsProperties.Focused`; [enabled] <- **absence** of
 * `SemanticsProperties.Disabled` (real Compose has no boolean "Enabled" key —
 * disabled is a `Unit` marker whose presence means disabled, despite the
 * task brief's own shorthand naming it "Enabled"); [selected] <-
 * `SemanticsProperties.Selected`; [hasOnClick] <- presence of
 * `SemanticsActions.OnClick`; [isSensitive] <- see [isSensitive]'s own kdoc
 * (docs/07 §5 rule 6's redaction marker).
 *
 * @param isVisible false when the node is off-screen relative to its own
 *   root's bounds, or carries `InvisibleToUser`/`HideFromAccessibility` —
 *   rule 1's "invisible" prune criterion, pre-reduced to a boolean by the
 *   collector so no bounds cross this boundary (see file kdoc).
 * @param isZeroSize true when the node's `boundsInRoot` has zero width or
 *   height — rule 1's "zero-size" prune criterion, same reduction.
 * @param isSensitive true when this field should be treated as a sensitive
 *   value class for docs/07 §5 rule 6's redaction — see `UiTreeCollector`'s
 *   kdoc for the two signals this is computed from (the `_sensitive` testTag
 *   suffix convention this task defines, and the real
 *   `SemanticsProperties.IsSensitiveData` marker when a screen sets it
 *   directly). [SemanticSnapshotBuilder] additionally applies a value-content
 *   pattern-match backstop that does not depend on this flag at all.
 */
public data class RawSemanticsNode(
    val id: Int,
    val testTag: String? = null,
    val textRuns: List<String> = emptyList(),
    val contentDescription: List<String> = emptyList(),
    val editableText: String? = null,
    val role: RawRole? = null,
    val isDialog: Boolean = false,
    val paneTitle: String? = null,
    val liveRegion: RawLiveRegion? = null,
    val collectionInfo: RawCollectionInfo? = null,
    val isListItem: Boolean = false,
    val toggleableState: RawToggleableState? = null,
    val focused: Boolean = false,
    val enabled: Boolean = true,
    val selected: Boolean? = null,
    val hasOnClick: Boolean = false,
    val hasProgressIndicator: Boolean = false,
    val isSensitive: Boolean = false,
    val isVisible: Boolean = true,
    val isZeroSize: Boolean = false,
    val children: List<RawSemanticsNode> = emptyList(),
)

/**
 * The walked tree for one capture — a FOREST, not a single tree, because a
 * dialog is a separate Compose window with its own `unmergedRootSemanticsNode`
 * (docs/07 §2.1). [roots] holds one entry per currently-attached window, in
 * attach order: index 0 is always the primary screen's root (attached first
 * and never detached while the screen is on-screen); any later entries are
 * dialog (or other popup-window) roots, appended as `UiTreeCollector.attachRoot`
 * observes them. `SemanticSnapshotBuilder` walks every root independently and
 * concatenates the resulting components — the flat `screen_context/v1`
 * `components` array has no separate notion of "which window a component came
 * from," matching docs/07 §3's canonical example, where the dialog's
 * component sits in the same flat list as the screen's own fields.
 */
public data class RawSemanticsTree(val roots: List<RawSemanticsNode>)
