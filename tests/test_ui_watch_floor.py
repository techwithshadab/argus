"""Watch floor behaviour that an officer's decisions depend on (U1, U2).

The UI is a single page with no build step, so these read the source. They pin two
things that were wrong in a way the officer could not see: the live stream stopped
for good after a session expiry or a rollout while the header still claimed it was
retrying, and review actions keyed off the selected vessel rather than the selected
alert, so a vessel with two open alerts could have the wrong one accepted.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "services/ui/index.html").read_text()
SCRIPT = "\n".join(re.findall(r"<script>(.*?)</script>", UI, re.S))


def test_streams_are_reopened_with_backoff_rather_than_left_dead():
    assert "function openStream(" in SCRIPT
    assert "STREAM_BACKOFF_MS" in SCRIPT
    supervisor = SCRIPT.split("function openStream(", 1)[1].split("\nfunction ", 1)[0]
    assert "setTimeout(connect" in supervisor
    assert "source.close()" in supervisor


def test_both_streams_go_through_the_supervisor():
    """A raw EventSource anywhere would be the one that silently stops."""
    assert "openStream('/stream'" in SCRIPT
    assert "openStream('/events'" in SCRIPT
    creations = re.findall(r"new EventSource\(", SCRIPT)
    assert len(creations) == 1, "only the supervisor may construct an EventSource"


def test_backoff_is_bounded_and_starts_quickly():
    values = re.search(r"STREAM_BACKOFF_MS = \[([^\]]+)\]", SCRIPT).group(1)
    delays = [int(v.strip()) for v in values.split(",")]
    assert delays == sorted(delays), "backoff must not shrink"
    assert delays[0] <= 2000, "a rollout should recover in about a second"
    assert delays[-1] <= 60000, "an unbounded wait is indistinguishable from dead"


def test_an_expired_session_asks_for_a_reload_instead_of_retrying_forever():
    assert "function signedOut(" in SCRIPT
    assert "sessionAlive(" in SCRIPT
    alive = SCRIPT.split("async function sessionAlive(", 1)[1].split("\nfunction ", 1)[
        0
    ]
    assert "401" in alive and "403" in alive
    assert 'id="signedOutBanner"' in UI
    assert "#signedOutBanner{" in UI, (
        "the banner needs styling or it reads as stray text"
    )
    assert (
        'role="status"' in UI.split('id="signedOutBanner"')[0][-60:]
        or 'role="status"' in UI.split('id="signedOutBanner"')[1][:60]
    )


def test_a_reconnect_clears_the_banner():
    supervisor = SCRIPT.split("function openStream(", 1)[1].split("\nfunction ", 1)[0]
    assert "onopen" in supervisor
    onopen = supervisor.split("onopen", 1)[1].split("\n", 1)[0]
    assert "hidden = true" in onopen and "attempt = 0" in onopen


def test_selection_is_by_alert_not_only_by_vessel():
    assert "selectedAlert:null" in SCRIPT
    assert "function selectAlert(" in SCRIPT


def test_the_keyboard_acts_on_the_selected_alert():
    handler = SCRIPT.split("document.addEventListener('keydown'", 1)[1].split("});", 1)[
        0
    ]
    assert "a.id===state.selectedAlert" in handler
    assert "a.mmsi===state.selected" not in handler, (
        "keying off the vessel picks the wrong card"
    )


def test_the_highlighted_card_is_the_one_that_gets_reviewed():
    """The class on the card and the row the keyboard finds must use the same key."""
    card = re.search(
        r'<div class="alert \$\{esc\(a\.severity\)\}([^"]*)"', SCRIPT
    ).group(1)
    assert "state.selectedAlert===a.id" in card
    select = SCRIPT.split("function selectVessel(", 1)[1].split("\nfunction ", 1)[0]
    assert "el.dataset.id === state.selectedAlert" in select


def test_clicking_a_card_selects_that_alert():
    """Through a delegated listener now, not an inline handler (U11)."""
    assert "alertAction(card, e.target.closest('button')?.dataset.act)" in SCRIPT
    action = SCRIPT.split("function alertAction(", 1)[1].split("\n}", 1)[0]
    assert "selectAlert(id, mmsi)" in action


def test_escape_clears_the_alert_selection_too():
    handler = SCRIPT.split("document.addEventListener('keydown'", 1)[1].split("});", 1)[
        0
    ]
    escape = handler.split("e.key==='Escape'", 1)[1].split("return;", 1)[0]
    assert "state.selectedAlert=null" in escape


def test_drawing_does_not_scan_the_alert_list_per_vessel():
    """U3: this was O(vessels x (alerts + investigations)) on every position event."""
    fn = SCRIPT.split("function vesselState(", 1)[1].split("\n\n", 1)[0]
    assert "marked.alerted.has(" in fn and "marked.investigating.has(" in fn
    assert ".some(" not in fn, "a per-vessel scan is what made 900 vessels unusable"


def test_the_marked_sets_are_rebuilt_when_those_lists_change():
    assert "function refreshMarked(" in SCRIPT
    for loader in ("loadAlerts", "loadInvestigations"):
        body = SCRIPT.split(f"function {loader}(", 1)[1].split("catch", 1)[0]
        assert "refreshMarked()" in body, loader


def test_positions_are_coalesced_into_one_draw_per_frame():
    assert "function scheduleVessels(" in SCRIPT
    fn = SCRIPT.split("function scheduleVessels(", 1)[1].split("\nfunction ", 1)[0]
    assert "requestAnimationFrame" in fn
    assert "drawPending" in fn, "without a guard every event still queues a draw"
    handler = SCRIPT.split("position: e =>", 1)[1].split("\n", 1)[0]
    assert "scheduleVessels()" in handler
    assert "renderVessels()" not in handler


def test_an_open_evidence_panel_survives_the_eight_second_refresh():
    """U4: the panel collapsed under the officer reading it."""
    assert "function rememberPanels(" in SCRIPT
    assert "openPanels" in SCRIPT
    assert 'data-panel="alert-${a.id}"' in SCRIPT
    render = SCRIPT.split("function renderAlerts(", 1)[1].split("\nfunction ", 1)[0]
    assert "rememberPanels(" in render


def test_panels_are_remembered_per_alert_not_globally():
    fn = SCRIPT.split("function rememberPanels(", 1)[1].split("\nfunction ", 1)[0]
    assert "d.dataset.panel" in fn
    assert "openPanels.add(" in fn and "openPanels.delete(" in fn


# ---- U5: one refresh path ----
def test_the_open_investigation_has_a_single_refresh_path():
    """The stream and the timer both fetched and rendered, so a completion painted twice."""
    assert "async function refreshInvestigation(" in SCRIPT
    assert SCRIPT.count("refreshInvestigation(") >= 3
    fn = SCRIPT.split("async function refreshInvestigation(", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "catch(e)" in fn, "a 401 used to throw every four seconds forever"
    assert "invRefreshing" in fn
    assert "state.openInv !== id" in fn, "a closed panel must not be repainted"


def test_the_poll_clears_itself_when_the_case_finishes():
    fn = SCRIPT.split("async function refreshInvestigation(", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert "clearInterval(state.poll)" in fn


# ---- U6: ageing ----
def test_an_alert_shows_its_age_and_its_date():
    assert "const ago = ts =>" in SCRIPT
    card = SCRIPT.split('<div class="meta">${dmy(a.started_at)}', 1)[1].split(
        "</div>", 1
    )[0]
    assert "ago(a.created_at)" in card
    assert "dmy(a.started_at)" in SCRIPT, "the window needs a date, not just a time"


# ---- U7: the filter never hides what the officer selected ----
def test_the_area_filter_keeps_selected_and_alerted_vessels():
    fn = SCRIPT.split("function renderVessels(", 1)[1].split("\nasync function ", 1)[0]
    assert "const keep = v =>" in fn
    assert "inArea(v, only) || keep(v)" in fn


# ---- U8: the narrow layout ----
def test_there_is_a_layout_for_a_narrow_screen():
    assert "@media (max-width:750px)" in UI
    narrow = UI.split("@media (max-width:750px)", 1)[1].split("@media", 1)[0]
    assert "grid-template-columns:1fr" in narrow
    assert "header{grid-column:1" in narrow, (
        "the header spans a column that no longer exists"
    )
    assert "min-height:280px" in narrow, "MapLibre needs a sized container"


def test_the_map_is_told_when_the_layout_changes():
    assert "map.resize()" in SCRIPT


# ---- U9: accessibility ----
def test_the_toasts_are_announced():
    assert 'id="toasts" role="status" aria-live="polite"' in UI


def test_the_severity_filters_are_real_controls():
    assert '<button type="button" class="chip high on"' in UI
    assert 'aria-pressed="true"' in UI
    assert "c.setAttribute('aria-pressed', String(on))" in SCRIPT


def test_the_alert_cards_are_reachable_by_keyboard():
    assert 'role="button" tabindex="0"' in SCRIPT
    assert "addEventListener('keydown'" in SCRIPT.split("$('alerts')", 1)[1]


def test_the_inputs_are_labelled():
    for el in ("fSearch", "auditSearch", "fSort", "fKind"):
        block = UI.split(f'id="{el}"', 1)[1][:200]
        assert "aria-label" in block, el


def test_reduced_motion_is_honoured_in_css_and_on_the_map():
    assert "@media (prefers-reduced-motion:reduce)" in UI
    assert "const flyMs = ms =>" in SCRIPT
    assert "duration:600" not in SCRIPT and "duration:700" not in SCRIPT


# ---- U10: officer features ----
def test_a_rejection_can_carry_its_reason():
    """The API has always accepted a note; the UI never sent one."""
    assert "function rejectAlert(" in SCRIPT
    fn = SCRIPT.split("function rejectAlert(", 1)[1].split("\n}", 1)[0]
    assert "reviewAlert(id, 'rejected', note" in fn
    assert "note !== null" in fn, "cancelling must cancel the review"


def test_both_reject_paths_go_through_the_same_function():
    assert "rejectAlert(a.id)" in SCRIPT, "the keyboard shortcut must prompt too"
    assert "reviewAlert(a.id,'rejected')" not in SCRIPT


def test_the_proposed_collection_area_is_drawn():
    """A2 proposed a collection over New York for a vessel off Singapore."""
    assert "map.addSource('aoi'" in SCRIPT
    assert "function renderAois(" in SCRIPT
    fn = SCRIPT.split("function renderAois(", 1)[1].split("\n}", 1)[0]
    assert "t.status==='proposed'" in fn and "t.aoi" in fn


def test_the_queue_can_be_filtered_by_kind_and_area():
    assert 'id="fKind"' in UI and 'id="fArea"' in UI
    fn = SCRIPT.split("function visibleAlerts(", 1)[1].split("\n}", 1)[0]
    assert "a.kind===f.kind" in fn
    assert "f.areaOnly" in fn


# ---- U11: no ids in handler strings ----
def test_ids_do_not_reach_the_dom_as_javascript():
    for gone in (
        'onclick="selectAlert',
        'onclick="reviewAlert',
        'onclick="toggleTrack(',
    ):
        assert gone not in SCRIPT, gone
    assert 'data-id="${esc(a.id)}"' in SCRIPT


def test_the_delegated_listeners_are_attached_once():
    """The list is replaced every eight seconds; a per-render listener would stack."""
    render = SCRIPT.split("function renderAlerts(", 1)[1].split("\nfunction ", 1)[0]
    assert "addEventListener" not in render


# ---- U12: guards ----
def test_a_null_score_is_not_reported_as_zero():
    assert "const pct = v =>" in SCRIPT
    assert "${pct(a.score)}" in SCRIPT
    assert "(a.score*100).toFixed(0)" not in SCRIPT


def test_the_score_sort_stays_consistent_with_a_null():
    fn = SCRIPT.split("function visibleAlerts(", 1)[1].split("\n}", 1)[0]
    assert "Number.isFinite(a.score) ? a.score : -1" in fn


def test_one_missing_network_node_does_not_hide_the_whole_graph():
    fn = SCRIPT.split("async function loadNetwork(", 1)[1].split("\nfunction ", 1)[0]
    assert "byId[id]?.name" in fn
    assert "byId[e.src].name" not in fn


# ---- U13: local filtering ----
def test_the_audit_search_filters_what_it_already_has():
    assert "$('auditSearch').oninput = renderAudit;" in SCRIPT
    assert "function renderAudit(" in SCRIPT
    fn = SCRIPT.split("function renderAudit(", 1)[1].split("\n$(", 1)[0]
    assert "get('/audit" not in fn, "typing must not refetch"
