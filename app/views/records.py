"""Records: browse, search, sort, edit, delete and add train stop records.

Every read and write goes through the access layer (access.py, writes.py), which picks the servers.
This page never mentions servers: that's the data transparency the spec asks about.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import streamlit as st

import access
import db
import ui
import writes
from access import Filters, Service, Sort

settings = ui.get_settings()
isolation = ui.isolation()

SORT_BY = {"ID": "stop_rdt_id", "Arrival time": "stop_arrival_time", "Delay": "stop_arrival_delay"}
EMPTY_NOTE = {"stop_arrival_time": "an arrival time", "stop_arrival_delay": "a delay"}
ANY = "Any"
UNREACHABLE = "The records can't be reached right now. Please try again in a moment."

state = st.session_state
state.setdefault("page_token", 0)  # changes whenever the page of rows changes, to clear the table's selection


# --- Messages that survive a rerun (e.g. "Saved" after the page reloads) -------------------------


def flash(kind: str, text: str) -> None:
    state["flash"] = (kind, text)


def show_flash() -> None:
    if "flash" in state:
        kind, text = state.pop("flash")
        getattr(st, kind)(text)


# --- Navigation ---------------------------------------------------------------------------------


def go(kind: str, anchor: object, page_no: int) -> None:
    """Remember which page to show next: "first", "after"/"before" a row, or "jump" to a number."""
    state["nav"] = (kind, anchor)
    state["page_no"] = page_no
    state["page_token"] += 1
    state.pop("mode", None)


def reload_page() -> None:
    """After a write: show the same page again, with fresh rows and nothing selected."""
    state["page_token"] += 1
    state.pop("mode", None)


# --- Input checks -------------------------------------------------------------------------------


def parse_delay(text: str) -> int | None:
    """Delay in whole minutes; empty means "no delay recorded". Raises ValueError with a message."""
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError("Delay must be a whole number of minutes, like 3 or -1 (or empty).") from None


def time_inputs(prefix: str, current: datetime | None) -> datetime | None:
    """Date and time inputs in Dutch time, plus a "has an arrival time" box. Returns the moment or None."""
    shown = (current or datetime.now(ui.LOCAL_TIME)).astimezone(ui.LOCAL_TIME)
    has_time = st.checkbox("Has an arrival time", value=current is not None or prefix.startswith("add"),
                           key=f"{prefix}_has_time")
    day_col, clock_col = st.columns(2)
    day = day_col.date_input("Arrival date (NL time)", value=shown.date(), key=f"{prefix}_day")
    clock = clock_col.time_input("Arrival time (NL time)", value=shown.time().replace(second=0, microsecond=0),
                                 step=60, key=f"{prefix}_clock")
    if not has_time:
        return None
    moment = ui.from_local(day, clock)
    # The inputs only go down to minutes: if the minute didn't change, keep the exact stored value.
    if current is not None and moment == current.replace(second=0, microsecond=0):
        return current
    return moment


def problem_message(error: Exception) -> str:
    if isinstance(error, writes.EditConflict):
        return "This record changed while you were editing, so nothing was saved. Open it again to see its current values."
    if isinstance(error, db.Refused):
        return f"The database refused this change ({error.reason}). Nothing was saved; please try again."
    return UNREACHABLE


# --- Page ---------------------------------------------------------------------------------------

st.title("Records")
show_flash()

try:
    types, companies = ui.value_lists(settings)
except access.Unavailable:
    types, companies = [], []
    st.warning("The type and company lists can't be loaded right now.")

# Search and sort
c_id, c_type, c_company, c_sort, c_order = st.columns([1, 1, 1, 1, 1])
id_text = c_id.text_input("ID", key="f_id", placeholder="any")
type_choice = c_type.selectbox("Type", [ANY, *types], key="f_type")
company_choice = c_company.selectbox("Company", [ANY, *companies], key="f_company")
sort_label = c_sort.selectbox("Sort by", list(SORT_BY), key="f_sort")
order = c_order.radio("Order", ["Ascending", "Descending"], key="f_order", horizontal=True)

if id_text.strip() and not id_text.strip().isdigit():
    st.error("ID must be a whole number.")
    st.stop()

filters = Filters(
    stop_rdt_id=int(id_text) if id_text.strip() else None,
    service_type=None if type_choice == ANY else type_choice,
    service_company=None if company_choice == ANY else company_choice,
)
sort = Sort(SORT_BY[sort_label], descending=order == "Descending")

# A new search or order starts again at page 1.
if state.get("view") != (filters, sort):
    state["view"] = (filters, sort)
    go("first", None, 1)

kind, anchor = state["nav"]
page_no = state["page_no"]
try:
    if kind == "jump":
        page = access.jump_to_page(settings, filters, anchor, sort, isolation=isolation)
    else:
        page = access.list_rows(
            settings, filters, sort,
            after=anchor if kind == "after" else None,
            before=anchor if kind == "before" else None,
            isolation=isolation,
        )
except access.Unavailable:
    st.error(UNREACHABLE)
    st.stop()
except db.Refused as error:
    st.error(f"The database refused this read ({error.reason}). Please try again.")
    st.stop()

if not page.has_previous and page_no != 1:  # e.g. rows were added or removed before this page
    page_no = state["page_no"] = 1

# Paging controls
first_col, prev_col, box_col, next_col = st.columns([1, 1, 1, 1])
first_col.button("⏮ First", on_click=go, args=("first", None, 1), disabled=page_no == 1 and kind == "first")
prev_col.button("◀ Previous", disabled=not (page.has_previous and page.rows),
                on_click=go, args=("before", page.rows[0] if page.rows else None, page_no - 1))
box_key = f"page_box_{state['page_token']}"
can_jump = access.can_jump(settings, filters)


def jump() -> None:
    number = int(state[box_key])
    go("jump", number, number)


box_col.number_input("Page", min_value=1, value=page_no, step=1, key=box_key, on_change=jump,
                     disabled=not can_jump, label_visibility="collapsed",
                     help="Type a page number to jump there." if can_jump
                     else "Jumping to a page isn't available right now; use Previous / Next.")
next_col.button("Next ▶", disabled=not (page.has_next and page.rows),
                on_click=go, args=("after", page.rows[-1] if page.rows else None, page_no + 1))

# The table
if not page.rows:
    st.info("No records match." if page_no == 1 else "This page is past the end of the list.")
    selected = None
else:
    table = st.dataframe(
        [
            {
                "ID": row.stop_rdt_id,
                "Type": row.service_type,
                "Company": row.service_company,
                "Arrival (NL time)": ui.show_time(row.stop_arrival_time),
                "Delay (min)": row.stop_arrival_delay,
            }
            for row in page.rows
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"table_{state['page_token']}",
        column_config={
            "ID": st.column_config.NumberColumn(format="%d"),
            "Delay (min)": st.column_config.NumberColumn(format="%d"),
        },
    )
    notes = [f"Page {page_no}. Click a row to open it. Clicking a column header only reorders the rows on this page."]
    if sort.column in EMPTY_NOTE:
        notes.append(f"Records without {EMPTY_NOTE[sort.column]} aren't listed when sorting by {sort_label.lower()}.")
    st.caption(" ".join(notes))
    picked = table.selection.rows
    selected = page.rows[picked[0]] if picked else None

# The opened record
if selected is not None:
    st.subheader(f"Record {selected.stop_rdt_id}")
    st.write(
        f"**Type:** {selected.service_type or '(none)'} · **Company:** {selected.service_company or '(none)'} · "
        f"**Arrival:** {ui.show_time(selected.stop_arrival_time) or '(none)'} · "
        f"**Delay:** {'(none)' if selected.stop_arrival_delay is None else f'{selected.stop_arrival_delay} min'}"
    )
    mode = state.get("mode")
    # The values the user sees when opening Edit/Delete are kept as "before": the save only goes
    # through if the record still holds them (otherwise someone else changed it in the meantime).
    if mode is None or mode[1].stop_rdt_id != selected.stop_rdt_id:
        edit_col, delete_col, _ = st.columns([1, 1, 4])
        if edit_col.button("Edit"):
            state["mode"] = ("edit", selected)
            st.rerun()
        if delete_col.button("Delete"):
            state["mode"] = ("delete", selected)
            st.rerun()

    elif mode[0] == "edit":
        before: Service = mode[1]
        with st.form("edit_form"):
            st.text_input("Type (can't be changed)", value=before.service_type or "", disabled=True)
            company = st.text_input("Company", value=before.service_company or "")
            # Keys include the record and page, so opening another record starts from its own values.
            arrival = time_inputs(f"edit_{before.stop_rdt_id}_{state['page_token']}", before.stop_arrival_time)
            delay_text = st.text_input("Delay in minutes (empty for none)",
                                       value="" if before.stop_arrival_delay is None else str(before.stop_arrival_delay))
            save_col, cancel_col, _ = st.columns([1, 1, 4])
            save = save_col.form_submit_button("Save", type="primary")
            cancel = cancel_col.form_submit_button("Cancel")
        if cancel:
            state.pop("mode", None)
            st.rerun()
        if save:
            try:
                after = replace(before, service_company=company.strip() or None, stop_arrival_time=arrival,
                                stop_arrival_delay=parse_delay(delay_text))
            except ValueError as error:
                st.error(str(error))
            else:
                if after == before:
                    st.info("Nothing changed.")
                else:
                    try:
                        writes.update_row(settings, before, after, isolation=isolation)
                    except (db.Refused, access.Unavailable) as error:
                        flash("error", problem_message(error))
                    else:
                        flash("success", f"Saved record {before.stop_rdt_id}.")
                    reload_page()
                    st.rerun()

    elif mode[0] == "delete":
        before = mode[1]
        st.warning(f"Delete record {before.stop_rdt_id}? This can't be undone.")
        yes_col, no_col, _ = st.columns([1.5, 1, 3.5])
        if yes_col.button("Yes, delete", type="primary"):
            try:
                writes.delete_row(settings, before, isolation=isolation)
            except (db.Refused, access.Unavailable) as error:
                message = problem_message(error)
                if isinstance(error, writes.EditConflict):
                    message = "This record changed or was already deleted since you opened it. Nothing was deleted."
                flash("error", message)
            else:
                flash("success", f"Deleted record {before.stop_rdt_id}.")
            reload_page()
            st.rerun()
        if no_col.button("Cancel"):
            state.pop("mode", None)
            st.rerun()

# Adding a record
with st.expander("Add a record"):
    if not types:
        st.write("The list of types can't be loaded right now, so records can't be added.")
    else:
        with st.form("add_form"):  # inputs stay filled in, so a rejected value can be corrected
            new_type = st.selectbox("Type", types, index=types.index("Sprinter") if "Sprinter" in types else 0)
            new_company = st.text_input("Company", value="NS")
            new_arrival = time_inputs("add", None)
            new_delay_text = st.text_input("Delay in minutes (empty for none)", value="0")
            add = st.form_submit_button("Add record", type="primary")
        if add:
            try:
                new_delay = parse_delay(new_delay_text)
            except ValueError as error:
                st.error(str(error))
            else:
                company = new_company.strip() or None
                try:
                    result = writes.insert_row(settings, new_type, company, new_arrival, new_delay, isolation=isolation)
                except (db.Refused, access.Unavailable) as error:
                    flash("error", problem_message(error))
                else:
                    flash("success", f"Added record {result.stop_rdt_id}. Search for its ID to open it.")
                    if company and company.lower() not in {c.lower() for c in companies}:
                        ui.value_lists.clear()  # a new company: refresh the dropdown list
                reload_page()
                st.rerun()
