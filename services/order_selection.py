from __future__ import annotations

from collections.abc import Iterable, Mapping


def _ids(values: Iterable) -> set[int]:
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def select_visible(existing: Iterable, visible: Iterable) -> list[int]:
    return sorted(_ids(existing) | _ids(visible))


def deselect_visible(existing: Iterable, visible: Iterable) -> list[int]:
    return sorted(_ids(existing) - _ids(visible))


def replace_visible_selection(
    existing: Iterable,
    visible: Iterable,
    selected_visible: Iterable,
) -> list[int]:
    """Persist hidden selections while replacing checkbox values on screen."""
    visible_ids = _ids(visible)
    return sorted((_ids(existing) - visible_ids) | _ids(selected_visible))


def apply_editor_checkbox_changes(
    existing: Iterable,
    row_ids: Iterable,
    editor_state: Mapping | None,
) -> list[int]:
    """Apply Streamlit data-editor checkbox changes to the saved ID set.

    ``st.data_editor`` stores changes by the visible row position, not by the
    database ID.  Translating those positions here prevents a rerun from
    rebuilding the table with stale checkbox values and losing the last click.
    """
    selected = _ids(existing)
    ordered_ids = list(row_ids)
    if not isinstance(editor_state, Mapping):
        return sorted(selected)

    edited_rows = editor_state.get("edited_rows", {})
    if not isinstance(edited_rows, Mapping):
        return sorted(selected)

    for raw_index, changes in edited_rows.items():
        if not isinstance(changes, Mapping) or "Seleziona" not in changes:
            continue
        try:
            row_index = int(raw_index)
            row_id = int(ordered_ids[row_index])
        except (IndexError, TypeError, ValueError):
            continue
        if bool(changes["Seleziona"]):
            selected.add(row_id)
        else:
            selected.discard(row_id)
    return sorted(selected)
