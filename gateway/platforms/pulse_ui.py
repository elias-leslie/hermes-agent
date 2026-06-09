"""Telegram-native Pulse UI helpers.

This module is intentionally small and file-backed. It provides the Pulse
onboarding/menu UI for the existing Telegram adapter via a `pulse:` callback
prefix. It does not run the briefing agent; cron jobs remain responsible for
brief generation and can resume once onboarding completes and the user selects
an activation posture.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

PULSE_DIR = Path.home() / ".hermes" / "pulse"
PROFILE_PATH = PULSE_DIR / "profile.json"
FEEDBACK_PATH = PULSE_DIR / "feedback.json"
STATE_PATH = PULSE_DIR / "state.json"
SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "pulse_feedback.py"
PULSE_ITEM_RE = re.compile(r"\b(P[0-9A-Fa-f]{8})\b")
# Line that contains a Pulse item ID anywhere on it. Used to detect per-item
# delivery units even when the agent wrote `**PID** (Source) — ...` or
# `• PID text` or any other inline shape. This is the shape most agent briefs
# actually use, and the prior list-marker-only regex missed it.
PULSE_ID_LINE_RE = re.compile(r"\bP[0-9A-Fa-f]{8}\b")
# Backwards-compatible strict shape: numbered/bulleted line that also contains
# an item ID. Kept for tests; the new splitter prefers PULSE_ID_LINE_RE.
PULSE_ITEM_LINE_RE = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*])\s+.*\bP[0-9A-Fa-f]{8}\b")
PULSE_SECTION_HEADING_RE = re.compile(r"^\s*#{2,3}\s+\S")
PULSE_HORIZONTAL_RULE_RE = re.compile(r"^\s*-{3,}\s*$")
# `**Header**` or `### header` followed by a body and a `IDs:` line — the
# market-mode and weekly-synthesis shape the agent uses for thematic blocks.
PULSE_IDS_LINE_RE = re.compile(r"^\s*IDs?\s*:\s*.*\bP[0-9A-Fa-f]{8}\b", re.IGNORECASE)
# Bold header line (e.g. `**🌍 Geopolitics & markets**`). Used to start a new
# themed block in market-mode briefs.
PULSE_BOLD_HEADER_RE = re.compile(r"^\s*\*\*[^*\n]{2,}\*\*\s*$")
MAX_ITEMS_WITH_BUTTONS = 20
# When splitting inline-ID items, attach up to this many context lines AFTER
# the ID line to keep the item readable in its own Telegram message.
MAX_ITEM_CONTEXT_LINES = 4

INTERESTS = [
    ("ai_models", "AI/frontier models"),
    ("ai_agents", "AI agents/automation"),
    ("devtools", "Software/dev tools"),
    ("cybersecurity", "Cybersecurity"),
    ("semiconductors", "Semiconductors/compute"),
    ("business", "Business/startups/VC"),
    ("markets", "Markets/macro"),
    ("crypto", "Crypto/digital assets"),
    ("us_policy", "US politics/policy"),
    ("geopolitics", "Geopolitics/conflict"),
    ("regions", "China/Russia/EU/Mideast"),
    ("energy_climate", "Energy/climate"),
    ("science_space", "Science/space"),
    ("health_biotech", "Health/biotech"),
    ("local", "Local/regional"),
    ("culture", "Culture/media"),
    ("sports", "Sports"),
    ("internet_trends", "Internet trends"),
    ("emergencies", "Emergencies only"),
]

LAYOUTS = {
    "daily": [
        ("executive", "Executive: 5 bullets"),
        ("balanced", "Balanced: 8-12 grouped"),
        ("deep", "Deep: 12-20 + why"),
        ("watchlist", "Watchlist only"),
    ],
    "weekly": [
        ("strategic", "Strategic synthesis"),
        ("reading", "Reading list"),
        ("scoreboard", "Scoreboard"),
    ],
    "emergency": [
        ("line", "One-line alert"),
        ("links", "Alert + links"),
        ("confidence", "Alert + confidence"),
    ],
}

COUNTS = [3, 5, 8, 12]

CORE_INTERVIEW_QUESTIONS = [
    {
        "id": "briefing_role",
        "text": "What job should Pulse optimize for most?",
        "options": [
            ("operator", "Operator: what needs a decision or action"),
            ("strategist", "Strategist: second-order consequences"),
            ("investor", "Investor: markets, companies, capital flows"),
            ("researcher", "Researcher: primary sources and technical detail"),
            ("general", "General awareness: concise world picture"),
        ],
    },
    {
        "id": "ranking_style",
        "text": "When space is limited, what should rank highest?",
        "options": [
            ("impact", "Highest real-world impact"),
            ("actionability", "Most actionable for me"),
            ("novelty", "Most new/surprising"),
            ("risk", "Largest downside/risk signal"),
            ("balanced", "Balanced mix"),
        ],
    },
    {
        "id": "source_posture",
        "text": "How should Pulse handle uncertain/breaking information?",
        "options": [
            ("primary_only", "Prefer primary/official sources; slower is OK"),
            ("trusted_confirmation", "Use trusted reports after confirmation"),
            ("fast_with_confidence", "Fast, but label confidence clearly"),
            ("watchlist_only", "Only surface if it affects selected watch areas"),
        ],
    },
    {
        "id": "emergency_threshold",
        "text": "What deserves an Emergency Pulse outside normal cadence?",
        "options": [
            ("safety_security", "Safety/security/geopolitical escalation"),
            ("markets_policy", "Major market, policy, or regulatory shocks"),
            ("ai_cyber", "Major AI/cybersecurity/compute events"),
            ("only_extreme", "Only extreme world-changing events"),
            ("none", "No emergency alerts for now"),
        ],
    },
    {
        "id": "noise_filter",
        "text": "What should Pulse aggressively filter out?",
        "options": [
            ("opinion", "Opinion/punditry unless tied to facts"),
            ("celebrity", "Celebrity/entertainment drama"),
            ("horse_race", "Political horse-race/process stories"),
            ("incremental", "Incremental/duplicate updates"),
            ("nothing", "Filter lightly; show me more"),
        ],
    },
]

CATEGORY_INTERVIEW_QUESTIONS = {
    "ai_models": {
        "id": "ai_models_focus",
        "text": "For frontier AI/models, what is most useful?",
        "options": [
            ("capabilities", "Capability jumps and benchmarks"),
            ("product", "Product launches and availability"),
            ("safety_policy", "Safety, regulation, and policy"),
            ("research", "Research papers and technical mechanisms"),
        ],
    },
    "ai_agents": {
        "id": "ai_agents_focus",
        "text": "For AI agents/automation, what should Pulse prioritize?",
        "options": [
            ("tools", "Practical tools I can use"),
            ("architecture", "Agent architectures/workflows"),
            ("business", "Agent startups and adoption"),
            ("risks", "Reliability, security, and failure modes"),
        ],
    },
    "cybersecurity": {
        "id": "cyber_focus",
        "text": "For cybersecurity, which alerts matter most?",
        "options": [
            ("exploited", "Actively exploited vulnerabilities"),
            ("supply_chain", "Supply-chain/package ecosystem risk"),
            ("ai_security", "AI/security intersection"),
            ("nation_state", "Nation-state/critical infrastructure"),
        ],
    },
    "semiconductors": {
        "id": "compute_focus",
        "text": "For semiconductors/compute, what angle should dominate?",
        "options": [
            ("supply", "Supply chain/capacity"),
            ("chips", "New chips/performance"),
            ("geopolitics", "Export controls/geopolitics"),
            ("datacenters", "Datacenters/power/AI infra"),
        ],
    },
    "markets": {
        "id": "markets_focus",
        "text": "For markets/macro, what should make the cut?",
        "options": [
            ("macro", "Rates, inflation, central banks"),
            ("equities", "Equities and major company moves"),
            ("credit", "Credit/liquidity stress"),
            ("crypto_linked", "Crypto and risk-asset spillovers"),
        ],
    },
    "us_policy": {
        "id": "us_policy_focus",
        "text": "For US policy/politics, what is worth surfacing?",
        "options": [
            ("policy_substance", "Policy substance and implementation"),
            ("regulation", "Regulation affecting tech/markets"),
            ("elections", "Election implications, not horse race"),
            ("institutions", "Courts, agencies, institutional shifts"),
        ],
    },
    "geopolitics": {
        "id": "geopolitics_focus",
        "text": "For geopolitics/conflict, what threshold should Pulse use?",
        "options": [
            ("escalation", "Escalation/de-escalation signals"),
            ("supply_chains", "Trade, energy, and supply-chain effects"),
            ("alliances", "Alliances/treaties/security posture"),
            ("humanitarian", "Humanitarian consequences"),
        ],
    },
    "regions": {
        "id": "regions_focus",
        "text": "For China/Russia/EU/Mideast, which lens matters most?",
        "options": [
            ("china", "China/US-China competition"),
            ("europe", "EU/NATO/Europe policy"),
            ("mideast", "Middle East/security/energy"),
            ("russia", "Russia/Ukraine and sanctions"),
        ],
    },
    "energy_climate": {
        "id": "energy_focus",
        "text": "For energy/climate, what should Pulse emphasize?",
        "options": [
            ("energy_prices", "Energy prices and supply"),
            ("grid_power", "Grid, power, datacenter constraints"),
            ("climate_risk", "Climate risk/extreme weather"),
            ("policy", "Policy, permits, transition economics"),
        ],
    },
    "science_space": {
        "id": "science_focus",
        "text": "For science/space, what deserves attention?",
        "options": [
            ("breakthroughs", "Major breakthroughs only"),
            ("space", "Space launches, missions, industry"),
            ("research", "High-quality papers and replications"),
            ("applications", "Commercial/practical applications"),
        ],
    },
    "health_biotech": {
        "id": "health_focus",
        "text": "For health/biotech, what matters most?",
        "options": [
            ("public_health", "Public health/outbreaks"),
            ("biotech", "Biotech/pharma breakthroughs"),
            ("longevity", "Longevity and wellness science"),
            ("policy", "Healthcare policy/regulation"),
        ],
    },
    "internet_trends": {
        "id": "internet_focus",
        "text": "For internet trends, what should not be missed?",
        "options": [
            ("platforms", "Platform shifts and creator economy"),
            ("memes", "Memes only if culturally/economically meaningful"),
            ("consumer", "Consumer apps/social behavior"),
            ("early_signals", "Early weak signals before mainstream coverage"),
        ],
    },
}



def extract_item_ids(text: str, *, limit: int = MAX_ITEMS_WITH_BUTTONS) -> list[str]:
    """Return unique Pulse item IDs in encounter order."""
    seen: set[str] = set()
    result: list[str] = []
    for match in PULSE_ITEM_RE.finditer(text or ""):
        item_id = match.group(1).upper()
        if item_id in seen:
            continue
        seen.add(item_id)
        result.append(item_id)
        if len(result) >= limit:
            break
    return result


def build_reply_markup_spec(text: str, *, item_ids: list[str] | None = None) -> dict[str, Any] | None:
    """Build a JSON-serializable Telegram inline-keyboard spec for a brief."""
    item_ids = item_ids if item_ids is not None else extract_item_ids(text)
    if not item_ids:
        return None
    rows: list[list[dict[str, str]]] = []
    for item_id in item_ids:
        rows.append([
            {"text": f"👍 {item_id}", "callback_data": f"pulse:up:{item_id}"},
            {"text": "👎", "callback_data": f"pulse:down:{item_id}"},
            {"text": "Hide", "callback_data": f"pulse:mute:{item_id}"},
            {"text": "Explain", "callback_data": f"pulse:explain:{item_id}"},
        ])
    return {"inline_keyboard": rows}


def metadata_for_brief(text: str) -> dict[str, Any] | None:
    """Return gateway metadata for Pulse brief buttons, or None."""
    markup = build_reply_markup_spec(text)
    if not markup:
        return None
    return {"reply_markup": markup}


def _metadata_for_item_block(block: str) -> dict[str, Any] | None:
    ids = extract_item_ids(block)
    if not ids:
        return None
    markup = build_reply_markup_spec(block, item_ids=ids)
    return {"reply_markup": markup} if markup else None


def _split_heading_sections_for_delivery(text: str) -> list[dict[str, Any]]:
    """Split markdown section reports so section feedback stays local.

    Splits on three boundaries, in priority order:

    - horizontal rules ``---`` (the canonical weekly/market block separator)
    - ``##``/``###`` markdown headings
    - bold header lines ``**Title**`` (the market-mode thematic header shape)

    A horizontal rule is dropped from the output (it is a separator, not
    content). A ``##``/``###`` heading or a bold header is **included** in
    the next section's buffer so the section title lands at the top of
    the unit and the user sees the full themed block.

    Each section that contains Pulse IDs gets its own delivery unit with
    an inline keyboard listing the IDs in that section. Sections without
    IDs (intro, position snapshot, footer) render as plain messages.
    """
    lines = text.splitlines()
    units: list[dict[str, Any]] = []
    buffer: list[str] = []
    saw_section_with_ids = False

    def flush() -> None:
        nonlocal buffer, saw_section_with_ids
        block = "\n".join(buffer).strip()
        buffer = []
        if not block:
            return
        metadata = _metadata_for_item_block(block)
        if metadata:
            saw_section_with_ids = True
        units.append({"text": block, "metadata": metadata})

    def is_section_title(line: str) -> bool:
        """Boundary lines that are also section titles — keep them in the
        next section's buffer instead of dropping them."""
        if PULSE_SECTION_HEADING_RE.match(line):
            return True
        if PULSE_BOLD_HEADER_RE.match(line):
            return True
        return False

    for line in lines:
        if PULSE_HORIZONTAL_RULE_RE.match(line):
            # Horizontal rule: flush, then drop the rule.
            if buffer:
                flush()
            continue
        if is_section_title(line) and buffer:
            # Section title: flush the previous buffer, then start the
            # next section WITH this title line as the first line.
            flush()
            buffer.append(line)
            continue
        buffer.append(line)
    flush()

    if not saw_section_with_ids:
        return []
    return units


def split_brief_for_delivery(text: str) -> list[dict[str, Any]]:
    """Split a Pulse brief so each item message carries its own buttons.

    Telegram inline keyboards are always rendered under the message they are
    attached to. A single keyboard for a full brief therefore appears grouped at
    the bottom. For scheduled Pulse briefs, split numbered/bulleted item blocks,
    inline-ID item lines, or markdown synthesis sections into separate delivery
    units so feedback controls appear directly under the relevant item/section
    while preserving header/footer text as plain messages.

    Three splitter strategies, tried in order, fall through to a single-message
    fallback so the user always sees at least one keyboard:

    1. ``_split_numbered_list_for_delivery`` — strict numbered/bullet items
       (back-compat for older briefs that wrote `1. PID text`).
    2. ``_split_inline_id_for_delivery`` — any line containing a Pulse ID,
       grouped with up to ``MAX_ITEM_CONTEXT_LINES`` following non-blank lines.
       Catches the **PID** (Source) — ... shape that the current agents emit.
    3. ``_split_heading_sections_for_delivery`` — `### heading` body `IDs:`
       shape used by weekly synthesis and market-mode themed blocks.
    4. Fallback: single message with the full-brief keyboard so the user at
       least sees a thumbs-up/down control, never a button-less wall of text.
    """
    if not text or not text.strip():
        return []

    # Strategy 1: numbered/bulleted item lines (existing behavior).
    numbered_units = _filter_junk_units(_split_numbered_list_for_delivery(text))
    if _count_units_with_buttons(numbered_units) > 1:
        return numbered_units

    # Strategy 2: any line that contains a Pulse ID (the shape the current
    # agents actually emit: `**PID** (Source) — ...`).
    inline_units = _filter_junk_units(_split_inline_id_for_delivery(text))
    if _count_units_with_buttons(inline_units) > 1:
        return inline_units

    # Strategy 3: heading-section split (weekly synthesis, market-mode blocks).
    section_units = _filter_junk_units(_split_heading_sections_for_delivery(text))
    if _count_units_with_buttons(section_units) > 1:
        return section_units

    # Fallback: at minimum, return a single message with the full-brief
    # keyboard. The user should always have *some* controls. If there are no
    # IDs at all, return plain text (no empty keyboard).
    if extract_item_ids(text):
        metadata = metadata_for_brief(text)
        return [{"text": text.strip(), "metadata": metadata}]
    return [{"text": text.strip(), "metadata": None}]


def _count_units_with_buttons(units: list[dict[str, Any]]) -> int:
    """Count delivery units that would render an inline keyboard."""
    return sum(1 for unit in units if unit.get("metadata"))


def _split_numbered_list_for_delivery(text: str) -> list[dict[str, Any]]:
    """Strategy 1: numbered/bulleted item lines (strict, back-compat)."""
    lines = text.splitlines()
    units: list[dict[str, Any]] = []
    plain_buffer: list[str] = []
    item_count = 0
    i = 0

    def flush_plain() -> None:
        nonlocal plain_buffer
        block = "\n".join(plain_buffer).strip()
        if block:
            units.append({"text": block, "metadata": None})
        plain_buffer = []

    while i < len(lines):
        line = lines[i]
        if PULSE_ITEM_LINE_RE.search(line):
            flush_plain()
            block_lines = [line]
            i += 1
            while i < len(lines) and lines[i].strip():
                if PULSE_ITEM_LINE_RE.search(lines[i]):
                    break
                block_lines.append(lines[i])
                i += 1
            block = "\n".join(block_lines).strip()
            metadata = _metadata_for_item_block(block)
            if metadata:
                item_count += 1
            units.append({"text": block, "metadata": metadata})
            while i < len(lines) and not lines[i].strip():
                i += 1
            continue

        plain_buffer.append(line)
        i += 1

    flush_plain()
    # Return the units with item_count embedded for the caller to count buttons.
    return units


def _split_inline_id_for_delivery(text: str) -> list[dict[str, Any]]:
    """Strategy 2: split on any line that contains a Pulse item ID.

    Two item shapes are supported:

    1. **Item-line shape** — ``**PID** (Source) — sentence. sentence. sentence.``
       The ID line is the start of a new item. The unit contains the ID line
       plus up to ``MAX_ITEM_CONTEXT_LINES`` following non-blank lines, then
       stops on the next ID line / break.
    2. **Section-with-IDs shape** — ``**Header**\n\nbody paragraphs\n\nIDs: PID1 PID2``
       The IDs line is the LAST line of a themed block. The unit absorbs the
       preceding plain-buffer content (up to the prior boundary) so the body
       and the IDs line land in one Telegram message with the keyboard
       rendered directly under the IDs line. The user sees the full themed
       block (header + body + IDs) followed by 👍/👎/Hide/Explain.

    Lines that don't carry an ID and don't belong to a pending block accumulate
    in a plain buffer. Each plain buffer flush is a plain (no-keyboard) unit
    used for the brief's intro, position snapshot, and footer.

    Filter rule: at the end, drop any unit whose text is just a horizontal
    rule or pure whitespace, so trailing ``---`` separators do not produce
    empty Telegram messages.
    """
    lines = text.splitlines()
    units: list[dict[str, Any]] = []
    plain_buffer: list[str] = []
    i = 0

    def flush_plain() -> None:
        nonlocal plain_buffer
        block = "\n".join(plain_buffer).strip()
        if block:
            units.append({"text": block, "metadata": None})
        plain_buffer = []

    def is_break(line: str) -> bool:
        if not line.strip():
            return True
        if PULSE_HORIZONTAL_RULE_RE.match(line):
            return True
        if PULSE_SECTION_HEADING_RE.match(line):
            return True
        if PULSE_BOLD_HEADER_RE.match(line):
            return True
        return False

    def _looks_like_session_header(line: str) -> bool:
        """True if a line looks like the brief's session-level header.

        Examples:

        - ``**Pulse Market Premarket — 2026-06-09**``
        - ``**Pulse Daily — Tue 2026-06-09**``
        - ``**Pulse Market End of Day — 2026-06-09**``
        - ``**Pulse Weekly — 2026-06-09**``
        - ``**Pulse Emergency — <event>**``

        The session header is a one-line title at the top of the brief
        and should not be absorbed into a themed block. It lives in its
        own plain-text unit above the first thematic section.
        """
        stripped = line.strip()
        if not (stripped.startswith("**") and stripped.endswith("**")):
            return False
        inner = stripped[2:-2].strip()
        lower = inner.lower()
        return lower.startswith("pulse ")

    def pop_section_body() -> tuple[list[str], bool, bool]:
        """Pop trailing body lines from the plain buffer to attach to a
        following ``IDs:`` line. Stops at the most recent boundary so we
        never absorb content that belongs to a different block. The
        boundary line itself (bold header / `##` heading) is included in
        the absorbed body so the section title lands inside the unit.

        A horizontal rule ``---`` is a HARD boundary — we never cross it
        backwards. The session-level intro (date header, position
        snapshot) lives above the first ``---`` and must stay in its own
        plain-text unit, not be absorbed into a section.

        A line that looks like a session header (``**Pulse <something>**``)
        is also a boundary: the session header belongs in its own plain
        unit and we never absorb it into a section. We return
        ``hit_session_header=True`` in that case so the caller can put
        the session header back into the plain buffer for a clean
        flush later.

        Returns ``(absorbed_lines, hit_section_header, hit_session_header)``.
        The absorbed lines are in source order (top-to-bottom).
        """
        nonlocal plain_buffer
        # We pop from the END of plain_buffer, but we want the absorbed
        # lines in source order, so we collect into a side buffer and
        # reverse at the end.
        popped: list[str] = []
        # First pass: pop a single trailing blank separator between the
        # body and the IDs line. A second blank line means we have
        # crossed into the next section.
        if plain_buffer and not plain_buffer[-1].strip():
            popped.append(plain_buffer.pop())
        if not plain_buffer:
            return list(reversed(popped)), False, False
        # If the line directly above the trailing blank is a hard
        # boundary (horizontal rule), do NOT cross it.
        if PULSE_HORIZONTAL_RULE_RE.match(plain_buffer[-1]):
            return list(reversed(popped)), False, False
        # Second pass: pop body content lines until we hit a boundary
        # (a bold header / `##` heading / horizontal rule) or run out of
        # buffer. A horizontal rule is hard-stop; we don't include it in
        # the absorbed body. A bold header / `##` heading IS the title
        # of this section and IS included.
        hit_section_header = False
        hit_session_header = False
        while plain_buffer:
            tail = plain_buffer[-1]
            if PULSE_HORIZONTAL_RULE_RE.match(tail):
                # Hard boundary. Drop everything absorbed so far.
                popped.clear()
                return [], False, False
            if PULSE_SECTION_HEADING_RE.match(tail) or PULSE_BOLD_HEADER_RE.match(tail):
                if _looks_like_session_header(tail):
                    # Session header — keep it in the plain buffer.
                    popped.clear()
                    return [], False, True
                # Section title — include it in the unit.
                popped.append(plain_buffer.pop())
                hit_section_header = True
                break
            popped.append(plain_buffer.pop())
        return list(reversed(popped)), hit_section_header, hit_session_header

    while i < len(lines):
        line = lines[i]
        ids_on_line = extract_item_ids(line)
        if ids_on_line:
            # If this is an `IDs:`-style aggregator line, pull the preceding
            # body back into the same unit so the keyboard lands at the
            # bottom of the whole section. The session-level header (if
            # any) stays in the plain buffer and flushes as its own
            # plain unit, so the user sees the brief title above the
            # first thematic block.
            if PULSE_IDS_LINE_RE.match(line):
                body_lines, _hit_header, hit_session = pop_section_body()
                if hit_session:
                    # Put the IDs line itself back into the plain buffer
                    # for the next flush attempt (the section body
                    # above the session header is in the plain buffer
                    # too, but the IDs line is the "title" of this
                    # orphaned block — easier to just attach it after a
                    # new flush once the body is rebuilt). For now,
                    # treat the IDs line as its own plain unit.
                    units.append({"text": line.strip(), "metadata": None})
                    i += 1
                    while i < len(lines) and not lines[i].strip():
                        i += 1
                    continue
                block_lines = body_lines + [line]
                block = "\n".join(block_lines).strip()
                metadata = _metadata_for_item_block(block)
                units.append({"text": block, "metadata": metadata})
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                continue

            # Plain item-line: flush prior plain buffer, then start a new
            # unit with up to MAX_ITEM_CONTEXT_LINES following context.
            flush_plain()
            block_lines = [line]
            j = i + 1
            context_lines = 0
            while j < len(lines) and context_lines < MAX_ITEM_CONTEXT_LINES:
                nxt = lines[j]
                if is_break(nxt):
                    break
                # Stop if the next non-blank line starts a NEW item (different
                # ID or IDs-line). We allow a same-line `IDs:` continuation
                # but not a paragraph that introduces another item.
                if PULSE_ID_LINE_RE.search(nxt):
                    break
                block_lines.append(nxt)
                context_lines += 1
                j += 1
            block = "\n".join(block_lines).strip()
            metadata = _metadata_for_item_block(block)
            units.append({"text": block, "metadata": metadata})
            # Skip blank lines between items so the next iteration lands on
            # the next ID line, not on whitespace.
            while j < len(lines) and not lines[j].strip():
                j += 1
            i = j
            continue

        plain_buffer.append(line)
        i += 1

    flush_plain()
    return _filter_junk_units(units)


def _filter_junk_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop units whose text is empty, whitespace, or just a horizontal rule.

    Without this, a brief that ends with ``---`` produces a trailing empty
    Telegram message. Horizontal rules are section separators in the source
    markdown; they should never be the *content* of a delivery unit.
    """
    cleaned: list[dict[str, Any]] = []
    for unit in units:
        text = (unit.get("text") or "").strip()
        if not text:
            continue
        if PULSE_HORIZONTAL_RULE_RE.match(text):
            continue
        cleaned.append(unit)
    return cleaned


def _load_recent_item(item_id: str) -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for key in ("recent_items", "items", "item_by_id"):
        value = state.get(key)
        if isinstance(value, dict) and isinstance(value.get(item_id), dict):
            return value[item_id]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and str(item.get("id") or item.get("item_id")).upper() == item_id:
                    return item
    return {}


def _format_explain_followup(item_id: str) -> str:
    """Build a substantive explain follow-up that does real investigation.

    Two paths converge here:

    - Telegram inline button tap (``pulse:explain:<ID>``) — runs in the gateway
      callback context, no main agent loop is available. We do a real
      investigation synchronously: pull the item from local state, fetch the
      source URL via curl, pull portfolio context, and compose a 4-8 sentence
      response. Falls back to a clear "what we have on file" snapshot if the
      fetch fails.
    - Chat command ``Pulse explain <ID>`` — handled by the live agent in the
      same chat session; the response below is also what the user sees first
      while the agent prepares a deeper turn.
    """
    item = _load_recent_item(item_id)
    if not item:
        return (
            f"🔍 Pulse explain {item_id}\n"
            f"No recent local context for this item (it may be older than the "
            f"keep-window or from a brief type that does not persist per-item state).\n"
            f"Reply with any follow-up and I will investigate live: web-search the "
            f"topic, pull the strongest primary source, and cross-check your portfolio "
            f"when relevant. `Pulse mute {item_id}` to silence future repeats."
        )

    title = (item.get("title") or item.get("headline") or "Untitled item").strip()
    source = (item.get("source") or item.get("source_id") or "unknown source").strip()
    url = (item.get("url") or "").strip()
    topics = item.get("topics") or []
    if isinstance(topics, list):
        topics_text = ", ".join(str(t) for t in topics if str(t).strip())
    else:
        topics_text = str(topics)
    published = (item.get("published") or "").strip()
    summary_hint = (item.get("summary_hint") or "").strip()
    source_score = item.get("source_score")
    score = item.get("score")
    score_line_parts = []
    if isinstance(source_score, (int, float)):
        score_line_parts.append(f"source_score={source_score:.2f}")
    if isinstance(score, (int, float)):
        score_line_parts.append(f"weighted_score={score:.2f}")
    score_line = " · ".join(score_line_parts)

    # Real investigation: fetch the source URL, then pull portfolio context
    # if the item is in an adjacent topic. Both have hard timeouts and degrade
    # gracefully so a flaky network never breaks the explain button.
    fetched_excerpt = _fetch_url_excerpt(url) if url else ""
    portfolio_block = _portfolio_impact_for_topics(topics_text) if _is_portfolio_adjacent(topics_text) else ""

    lines = [f"🔍 Pulse explain {item_id}"]
    lines.append(f"Title: {title}")
    if topics_text:
        lines.append(f"Topics: {topics_text}")
    lines.append(f"Source: {source}" + (f"  ·  {score_line}" if score_line else ""))
    if published:
        lines.append(f"Published: {published}")

    if fetched_excerpt:
        lines.append("")
        lines.append("What the source says:")
        lines.append(fetched_excerpt)
    elif summary_hint:
        lines.append("")
        lines.append(f"What we have on file: {summary_hint}")
    if url:
        lines.append(f"URL: {url}")

    if portfolio_block:
        lines.append("")
        lines.append("Held-position read:")
        lines.append(portfolio_block)

    lines.append("")
    lines.append(
        "Reply in chat with a follow-up (e.g. \"why does this matter for VGT?\" or "
        "\"what changed since the last brief?\") and I will pull the source, "
        "corroborate with a second outlet, and check your held positions. "
        f"`Pulse mute {item_id}` to silence future repeats; `Pulse up {item_id}` to "
        "see more like this."
    )
    return "\n".join(lines)


def _fetch_url_excerpt(url: str, *, max_chars: int = 1200, timeout: int = 5) -> str:
    """Fetch a URL with curl and return a short text excerpt.

    Hard timeout so a slow server cannot stall the Telegram callback handler.
    Returns an empty string on any failure — callers must degrade gracefully.
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sL",
                "--max-time",
                str(timeout),
                "-A",
                "Mozilla/5.0 (compatible; HermesPulse/1.0)",
                url,
            ],
            text=True,
            capture_output=True,
            timeout=timeout + 2,
            check=False,
        )
    except Exception as exc:
        logger.debug("Pulse explain: curl failed for %s: %s", url, exc)
        return ""
    if proc.returncode != 0 or not proc.stdout:
        return ""
    # Strip tags, collapse whitespace, truncate. Good enough for a 1-2 sentence
    # excerpt; the agent turn can do a deeper fetch.
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", proc.stdout, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) > max_chars:
        # Cut on a sentence boundary near the cap for readability.
        cut = text[:max_chars]
        last_period = max(cut.rfind(". "), cut.rfind("。 "))
        if last_period > max_chars // 2:
            cut = cut[: last_period + 1]
        text = cut.rstrip() + "…"
    return text


def _is_portfolio_adjacent(topics_text: str) -> bool:
    """True if the topics suggest market/company/macro/cyber relevance."""
    if not topics_text:
        return False
    keywords = (
        "markets", "macro", "equities", "credit", "semiconductors",
        "energy_climate", "energy", "geopolitics", "us_policy", "policy",
        "finance", "cybersecurity", "cyber", "business", "ai_models",
        "ai_agents",
    )
    lower = topics_text.lower()
    return any(k in lower for k in keywords)


def _portfolio_impact_for_topics(topics_text: str) -> str:
    """Read positions and quote a 1-2 line held-position impact.

    Falls back to an empty string if the portfolio tool is unavailable.
    """
    try:
        proc = subprocess.run(
            ["st", "portfolio", "briefing-context", "--limit", "10", "--catalyst-days", "14"],
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception as exc:
        logger.debug("Pulse explain: st portfolio failed: %s", exc)
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        return ""
    # Trim to the most relevant signal: top 4 lines / first 600 chars.
    snippet = proc.stdout.strip()
    if len(snippet) > 600:
        snippet = snippet[:600].rstrip() + "…"
    return snippet


async def dispatch_callback(data: str) -> tuple[bool, str, str | None]:
    """Handle scheduled-brief feedback callbacks like pulse:up:PABC12345."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "pulse":
        return False, "Bad Pulse action.", None
    action, item_id = parts[1], parts[2].upper()
    if action not in {"up", "down", "mute", "explain"} or not PULSE_ITEM_RE.fullmatch(item_id):
        return False, "Bad Pulse action.", None
    if action == "explain":
        return True, "Opened item context.", _format_explain_followup(item_id)
    command_action = "mute" if action == "mute" else action
    try:
        proc = subprocess.run(
            ["python3", str(SCRIPT_PATH), "Pulse", command_action, item_id, "telegram button"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        logger.warning("Pulse feedback callback failed for %s %s: %s", action, item_id, exc)
        return False, "Pulse feedback failed.", None
    if proc.returncode != 0:
        logger.warning("Pulse feedback command failed rc=%s stderr=%s", proc.returncode, proc.stderr[-500:])
        return False, "Pulse feedback failed.", None
    label = {"up": "Saved thumbs up", "down": "Saved thumbs down", "mute": "Hidden"}.get(action, "Saved")
    return True, f"{label}: {item_id}", None

def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _profile() -> dict[str, Any]:
    return _load(PROFILE_PATH, {})


def _feedback() -> dict[str, Any]:
    return _load(FEEDBACK_PATH, {})


def _note(fb: dict[str, Any], text: str) -> None:
    fb.setdefault("notes", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "source": "telegram_ui",
    })


def _selected(profile: dict[str, Any]) -> set[str]:
    return set((profile.get("onboarding", {}) or {}).get("selected_interests", []))


def _set_selected(profile: dict[str, Any], selected: set[str]) -> None:
    profile.setdefault("onboarding", {})["selected_interests"] = sorted(selected)
    profile["interests"] = {key: 3 for key in sorted(selected)}


def _main_keyboard(profile: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    profile = profile or {}
    if profile.get("status") == "active":
        start_data = "pulse:onb:interview"
        start_label = "🧭 Review/edit interview"
    elif profile.get("status") == "onboarding_interview_pending" or profile.get("onboarding", {}).get("interview"):
        start_data = "pulse:onb:interview"
        start_label = "🧭 Continue interview"
    else:
        start_data = "pulse:onb:interests"
        start_label = "🧭 Start/continue onboarding"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(start_label, callback_data=start_data)],
        [InlineKeyboardButton("🎛 Layouts", callback_data="pulse:layouts"), InlineKeyboardButton("🔢 Article counts", callback_data="pulse:counts")],
        [InlineKeyboardButton("📊 Status", callback_data="pulse:status"), InlineKeyboardButton("⏸ Keep paused", callback_data="pulse:pause")],
    ])


def _interview_questions(profile: dict[str, Any]) -> list[dict[str, Any]]:
    selected = _selected(profile)
    questions = list(CORE_INTERVIEW_QUESTIONS)
    for key, _label in INTERESTS:
        if key in selected and key in CATEGORY_INTERVIEW_QUESTIONS:
            questions.append(CATEGORY_INTERVIEW_QUESTIONS[key])
    questions.append({
        "id": "final_activation_posture",
        "text": "After this interview, how should activation work?",
        "options": [
            ("review_first", "Show me a review summary before resuming crons"),
            ("activate_daily_weekly", "Resume daily + weekly after summary"),
            ("activate_all", "Resume daily + weekly + emergency after summary"),
            ("keep_paused", "Keep everything paused until I explicitly say go"),
        ],
    })
    return questions


def _interview_state(profile: dict[str, Any]) -> dict[str, Any]:
    onboarding = profile.setdefault("onboarding", {})
    state = onboarding.setdefault("interview", {})
    state.setdefault("current", 0)
    state.setdefault("answers", {})
    state.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    return state


def _answer_values(profile: dict[str, Any], question_id: str) -> list[str]:
    raw = _interview_state(profile).get("answers", {}).get(question_id)
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _set_answer_values(profile: dict[str, Any], question_id: str, values: list[str]) -> None:
    state = _interview_state(profile)
    clean = []
    for value in values:
        value = str(value).strip()
        if value and value not in clean:
            clean.append(value)
    answers = state.setdefault("answers", {})
    if clean:
        answers[question_id] = clean
        state.setdefault("answered_at", {})[question_id] = datetime.now(timezone.utc).isoformat()
    else:
        answers.pop(question_id, None)


def _toggle_answer_value(profile: dict[str, Any], question_id: str, value: str) -> bool:
    values = _answer_values(profile, question_id)
    if value in values:
        values.remove(value)
        selected = False
    else:
        values.append(value)
        selected = True
    _set_answer_values(profile, question_id, values)
    return selected


def _custom_answer_label(profile: dict[str, Any], question_id: str) -> str | None:
    custom = (_interview_state(profile).get("custom_answers", {}) or {}).get(question_id)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return None


def _answer_label(profile: dict[str, Any], question: dict[str, Any], answer_key: str) -> str:
    if answer_key == f"custom:{question['id']}":
        return _custom_answer_label(profile, question["id"]) or "Custom write-in"
    return dict(question["options"]).get(answer_key, answer_key)


def _answer_labels(profile: dict[str, Any], question: dict[str, Any]) -> list[str]:
    return [_answer_label(profile, question, key) for key in _answer_values(profile, question["id"])]


def _activation_values(profile: dict[str, Any]) -> list[str]:
    return _answer_values(profile, "final_activation_posture")


def _pulse_jobs_for_activation(profile: dict[str, Any]) -> list[str]:
    values = set(_activation_values(profile))
    if "activate_all" in values:
        return ["Pulse Daily", "Pulse Weekly", "Pulse Emergency"]
    if "activate_daily_weekly" in values:
        return ["Pulse Daily", "Pulse Weekly"]
    return []


def _resume_pulse_jobs_for_activation(profile: dict[str, Any]) -> dict[str, Any]:
    requested = _activation_values(profile)
    job_names = _pulse_jobs_for_activation(profile)
    result: dict[str, Any] = {"requested": requested, "resumed_jobs": [], "missing_jobs": [], "errors": []}
    if not job_names:
        return result
    try:
        from cron.jobs import resume_job
    except Exception as exc:
        result["errors"].append(f"cron import failed: {exc}")
        return result
    for name in job_names:
        try:
            job = resume_job(name)
        except Exception as exc:
            result["errors"].append(f"{name}: {exc}")
            continue
        if job:
            result["resumed_jobs"].append(job.get("name") or name)
        else:
            result["missing_jobs"].append(name)
    result["at"] = datetime.now(timezone.utc).isoformat()
    return result


def _pause_pulse_jobs(reason: str = "Pulse paused from Telegram UI") -> dict[str, Any]:
    result: dict[str, Any] = {"paused_jobs": [], "missing_jobs": [], "errors": []}
    try:
        from cron.jobs import pause_job
    except Exception as exc:
        result["errors"].append(f"cron import failed: {exc}")
        return result
    for name in ["Pulse Daily", "Pulse Weekly", "Pulse Emergency"]:
        try:
            job = pause_job(name, reason=reason)
        except Exception as exc:
            result["errors"].append(f"{name}: {exc}")
            continue
        if job:
            result["paused_jobs"].append(job.get("name") or name)
        else:
            result["missing_jobs"].append(name)
    result["at"] = datetime.now(timezone.utc).isoformat()
    return result


def _completion_text(profile: dict[str, Any], activation_result: dict[str, Any]) -> str:
    lines = [_interview_summary(profile), ""]
    resumed = activation_result.get("resumed_jobs") or []
    missing = activation_result.get("missing_jobs") or []
    errors = activation_result.get("errors") or []
    if resumed:
        lines.append("Activation: resumed " + ", ".join(resumed) + ".")
    elif "keep_paused" in set(_activation_values(profile)):
        lines.append("Activation: Pulse remains paused because you selected keep paused.")
    elif "review_first" in set(_activation_values(profile)):
        lines.append("Activation: review summary shown; Pulse remains paused until you explicitly resume it.")
    else:
        lines.append("Activation: no Pulse cron jobs were resumed.")
    if missing:
        lines.append("Missing Pulse jobs: " + ", ".join(missing) + ".")
    if errors:
        lines.append("Activation errors: " + "; ".join(errors))
    return "\n".join(lines)


def _interview_keyboard(profile: dict[str, Any]) -> InlineKeyboardMarkup:
    questions = _interview_questions(profile)
    state = _interview_state(profile)
    idx = min(max(int(state.get("current", 0)), 0), max(len(questions) - 1, 0))
    q = questions[idx]
    rows = []
    selected_answers = set(_answer_values(profile, q["id"]))
    for opt_idx, (key, label) in enumerate(q["options"]):
        mark = "✅" if key in selected_answers else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"pulse:onb:answer:{idx}:{opt_idx}")])
    custom_label = _custom_answer_label(profile, q["id"])
    custom_key = f"custom:{q['id']}"
    custom_mark = "✅" if custom_key in selected_answers else "☐"
    custom_text = f"{custom_mark} Other / write in"
    if custom_label and custom_key in selected_answers:
        custom_text = f"✅ Other: {custom_label[:42]}"
    rows.append([InlineKeyboardButton(custom_text, callback_data=f"pulse:onb:writein:{idx}")])
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("← Back", callback_data=f"pulse:onb:q:{idx - 1}"))
    if selected_answers:
        if idx < len(questions) - 1:
            nav.append(InlineKeyboardButton("Next →", callback_data=f"pulse:onb:q:{idx + 1}"))
        else:
            nav.append(InlineKeyboardButton("Finish interview", callback_data="pulse:onb:complete_interview"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("💬 Discuss this", callback_data=f"pulse:onb:discuss:{idx}")])
    rows.append([InlineKeyboardButton("📊 Status", callback_data="pulse:status"), InlineKeyboardButton("← Main", callback_data="pulse:main")])
    return InlineKeyboardMarkup(rows)


def _discussion_keyboard(profile: dict[str, Any], idx: int) -> InlineKeyboardMarkup:
    questions = _interview_questions(profile)
    idx = min(max(idx, 0), max(len(questions) - 1, 0))
    q = questions[idx]
    discussion = _interview_state(profile).get("discussion", {})
    pending = discussion.get("pending_answer")
    show_proposals = bool(discussion.get("show_proposals"))
    rows = [
        [InlineKeyboardButton("🤖 Explain / compare options", callback_data=f"pulse:onb:explain:{idx}")],
        [InlineKeyboardButton("✍️ I’ll type my question", callback_data=f"pulse:onb:type:{idx}")],
        [InlineKeyboardButton("📝 Other / write in final answer", callback_data=f"pulse:onb:writein:{idx}")],
    ]
    if show_proposals:
        for opt_idx, (key, label) in enumerate(q["options"]):
            mark = "🟡" if pending == key else "☐"
            rows.append([InlineKeyboardButton(f"{mark} Propose: {label}", callback_data=f"pulse:onb:propose:{idx}:{opt_idx}")])
        if pending:
            rows.append([
                InlineKeyboardButton("✅ Confirm final answer", callback_data=f"pulse:onb:confirm_discuss:{idx}"),
                InlineKeyboardButton("✏️ Keep discussing", callback_data=f"pulse:onb:type:{idx}"),
            ])
    else:
        rows.append([InlineKeyboardButton("🎯 Show proposal buttons", callback_data=f"pulse:onb:show_proposals:{idx}")])
    rows.append([InlineKeyboardButton("← Back to question", callback_data=f"pulse:onb:q:{idx}")])
    return InlineKeyboardMarkup(rows)


def _discussion_text(profile: dict[str, Any], idx: int) -> str:
    questions = _interview_questions(profile)
    idx = min(max(idx, 0), max(len(questions) - 1, 0))
    q = questions[idx]
    discussion = _interview_state(profile).get("discussion", {})
    turns = discussion.get("turns", [])[-4:]
    lines = [
        f"Pulse discussion for question {idx + 1}/{len(questions)}",
        "",
        q["text"],
        "",
        "Options:",
    ]
    lines.extend(f"• {label}" for _key, label in q["options"])
    lines.extend([
        "",
        "How to discuss: type your clarification/question in the Telegram message box below. I will answer as the Hermes agent. Your typed messages are discussion only — they never save a final answer.",
        "",
        "Use 🤖 Explain / compare options for an automatic agent explanation, or 🎯 Show proposal buttons only when you are ready to pick a candidate final answer. Confirm saves only after you explicitly approve it.",
    ])
    if turns:
        lines.append("")
        lines.append("Recent discussion:")
        for turn in turns:
            lines.append(f"You: {turn.get('user', '')}")
            lines.append(f"Pulse: {turn.get('assistant', '')}")
    pending = discussion.get("pending_answer")
    if pending:
        label = _answer_label(profile, q, pending)
        lines.extend(["", f"Pending proposal, not saved yet: {label}"])
    if discussion.get("awaiting_write_in"):
        lines.extend(["", "Write-in mode: type your custom final answer in the Telegram message box. I will show it back for confirmation before saving."])
    return "\n".join(lines)


def _discussion_prompt(profile: dict[str, Any], idx: int, user_text: str) -> str:
    questions = _interview_questions(profile)
    idx = min(max(idx, 0), max(len(questions) - 1, 0))
    q = questions[idx]
    discussion = _interview_state(profile).get("discussion", {})
    option_lines = "\n".join(f"- {label}" for _key, label in q["options"])
    pending = discussion.get("pending_answer_label")
    recent = discussion.get("turns", [])[-6:]
    recent_lines = []
    for turn in recent:
        if turn.get("user"):
            recent_lines.append(f"User: {turn['user']}")
    return (
        "You are Pulse onboarding, discussing one interview question with the user.\n"
        "This is a clarification conversation, not an answer submission.\n"
        "Do not save, infer, or claim a final answer.\n"
        "Do not tell the user you selected anything.\n"
        "Explain tradeoffs, ask follow-up questions when needed, and help the user decide.\n"
        "If the current answer options are inadequate, propose better wording and tell the user they can use Other / write in; it still requires explicit confirmation before being saved.\n"
        "If the user seems ready, say which option(s) sound closest, or suggest a write-in, and ask them to select all applicable checkboxes or use Other / write in, then confirm in Telegram.\n"
        "Keep the response concise but genuinely conversational.\n\n"
        f"Question {idx + 1}/{len(questions)}:\n{q['text']}\n\n"
        f"Available final options:\n{option_lines}\n\n"
        f"Pending proposed option, not saved: {pending or 'none'}\n\n"
        + ("Recent discussion turns:\n" + "\n".join(recent_lines) + "\n\n" if recent_lines else "")
        + f"User's latest discussion message:\n{user_text}"
    )


def _record_discussion_turn(profile: dict[str, Any], idx: int, text: str) -> str:
    state = _interview_state(profile)
    discussion = state.setdefault("discussion", {})
    discussion["active"] = True
    discussion["question_index"] = idx
    discussion.setdefault("turns", []).append({
        "at": datetime.now(timezone.utc).isoformat(),
        "user": text,
        "assistant": None,
    })
    return _discussion_prompt(profile, idx, text)


def _enqueue_discussion_agent_turn(adapter: Any, query: Any, profile: dict[str, Any], idx: int, text: str) -> None:
    from gateway.platforms.base import MessageEvent, MessageType

    message = query.message
    chat = message.chat
    user = query.from_user
    telegram_chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
    chat_type = "dm"
    if telegram_chat_type in {"group", "supergroup"}:
        chat_type = "group"
    elif telegram_chat_type == "channel":
        chat_type = "channel"
    thread_id = getattr(message, "message_thread_id", None)
    is_topic_message = bool(getattr(message, "is_topic_message", False))
    thread_id_str = str(thread_id) if thread_id is not None and is_topic_message else None
    source = adapter.build_source(
        chat_id=str(chat.id),
        chat_name=getattr(chat, "title", None) or getattr(chat, "full_name", None),
        chat_type=chat_type,
        user_id=str(user.id) if user else str(chat.id),
        user_name=getattr(user, "full_name", None) if user else getattr(chat, "full_name", None),
        thread_id=thread_id_str,
        message_id=str(getattr(message, "message_id", "")),
    )
    prompt = _record_discussion_turn(profile, idx, text)
    _save(PROFILE_PATH, profile)
    event = MessageEvent(
        text=prompt,
        message_type=MessageType.TEXT,
        source=source,
        raw_message=message,
        message_id=str(getattr(message, "message_id", "")),
    )
    adapter._enqueue_text_event(event)


def handle_pulse_write_in_message(adapter: Any, msg: Any) -> bool:
    profile = _profile()
    state = (profile.get("onboarding", {}) or {}).get("interview", {}) or {}
    discussion = state.get("discussion", {}) or {}
    if not discussion.get("awaiting_write_in"):
        return False
    chat_id = str(getattr(msg, "chat_id", ""))
    discussion_chat_id = discussion.get("chat_id")
    if discussion_chat_id and str(discussion_chat_id) != chat_id:
        return False
    text = (getattr(msg, "text", "") or "").strip()
    if not text or text.startswith("/"):
        return False
    questions = _interview_questions(profile)
    idx = min(max(int(discussion.get("question_index", state.get("current", 0)) or 0), 0), max(len(questions) - 1, 0))
    q = questions[idx]
    custom_key = f"custom:{q['id']}"
    state.setdefault("custom_answers", {})[q["id"]] = text
    discussion["active"] = True
    discussion["question_index"] = idx
    discussion["pending_answer"] = custom_key
    discussion["pending_answer_label"] = text
    discussion["pending_custom"] = True
    discussion["show_proposals"] = True
    discussion["awaiting_write_in"] = False
    discussion["pending_at"] = datetime.now(timezone.utc).isoformat()
    fb = _feedback()
    _note(fb, f"interview_write_in_pending question_index={idx}")
    _save(PROFILE_PATH, profile)
    _save(FEEDBACK_PATH, fb)
    return True


async def send_pulse_write_in_confirmation(adapter: Any, msg: Any) -> None:
    profile = _profile()
    state = _interview_state(profile)
    discussion = state.get("discussion", {})
    idx = int(discussion.get("question_index", state.get("current", 0)) or 0)
    await adapter._bot.send_message(
        chat_id=msg.chat_id,
        text=_discussion_text(profile, idx),
        reply_markup=_discussion_keyboard(profile, idx),
    )


def build_pulse_discussion_agent_prompt(msg: Any) -> str | None:
    profile = _profile()
    state = (profile.get("onboarding", {}) or {}).get("interview", {}) or {}
    discussion = state.get("discussion", {}) or {}
    if not discussion.get("active"):
        return None
    chat_id = str(getattr(msg, "chat_id", ""))
    discussion_chat_id = discussion.get("chat_id")
    if discussion_chat_id and str(discussion_chat_id) != chat_id:
        return None
    idx = int(discussion.get("question_index", state.get("current", 0)) or 0)
    text = (getattr(msg, "text", "") or "").strip()
    if not text or text.startswith("/"):
        return None
    fb = _feedback()
    prompt = _record_discussion_turn(profile, idx, text)
    _note(fb, f"interview_discuss_agent_turn question_index={idx}")
    _save(PROFILE_PATH, profile)
    _save(FEEDBACK_PATH, fb)
    return prompt


def _interview_text(profile: dict[str, Any]) -> str:
    questions = _interview_questions(profile)
    state = _interview_state(profile)
    idx = min(max(int(state.get("current", 0)), 0), max(len(questions) - 1, 0))
    answered = sum(1 for q in questions if _answer_values(profile, q["id"]))
    selected_labels = [label for key, label in INTERESTS if key in _selected(profile)]
    return (
        f"Pulse interview ({idx + 1}/{len(questions)})\n"
        f"Answered: {answered}/{len(questions)}\n"
        f"Selected areas: {', '.join(selected_labels[:6])}"
        + ("…" if len(selected_labels) > 6 else "")
        + "\n\n"
        + questions[idx]["text"]
    )


def _interview_summary(profile: dict[str, Any]) -> str:
    questions = _interview_questions(profile)
    lines = ["Pulse interview complete. Review summary:", ""]
    for q in questions:
        labels = _answer_labels(profile, q)
        if labels:
            lines.append(f"• {q['text']} {', '.join(labels)}")
    lines.extend([
        "",
        "Activation follows your final interview answer. You can still change layouts, article counts, interests, or pause Pulse from the menu.",
    ])
    return "\n".join(lines)


def _interest_keyboard(profile: dict[str, Any]) -> InlineKeyboardMarkup:
    selected = _selected(profile)
    rows = []
    for key, label in INTERESTS:
        mark = "✅" if key in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"pulse:onb:toggle:{key}")])
    rows.append([
        InlineKeyboardButton("Next: layouts →", callback_data="pulse:layouts"),
        InlineKeyboardButton("Done later", callback_data="pulse:main"),
    ])
    return InlineKeyboardMarkup(rows)


def _layout_keyboard(profile: dict[str, Any]) -> InlineKeyboardMarkup:
    prefs = profile.setdefault("layout_preferences", {})
    rows = []
    for cadence, opts in LAYOUTS.items():
        rows.append([InlineKeyboardButton(f"— {cadence.upper()} —", callback_data="pulse:noop")])
        current = prefs.get(cadence)
        for key, label in opts:
            mark = "✅" if current == key else "☐"
            rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"pulse:layout:{cadence}:{key}")])
    rows.append([InlineKeyboardButton("Next: article counts →", callback_data="pulse:counts")])
    rows.append([InlineKeyboardButton("← Main", callback_data="pulse:main")])
    return InlineKeyboardMarkup(rows)


def _count_keyboard(profile: dict[str, Any]) -> InlineKeyboardMarkup:
    prefs = profile.setdefault("article_count_preferences", {})
    rows = []
    for cadence in ("daily", "weekly"):
        rows.append([InlineKeyboardButton(f"— {cadence.upper()} —", callback_data="pulse:noop")])
        current = prefs.get(cadence)
        row = []
        for count in COUNTS:
            mark = "✅" if current == count else "☐"
            row.append(InlineKeyboardButton(f"{mark} {count}", callback_data=f"pulse:count:{cadence}:{count}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("Finish onboarding", callback_data="pulse:onb:finish")])
    rows.append([InlineKeyboardButton("← Main", callback_data="pulse:main")])
    return InlineKeyboardMarkup(rows)


def _status_text(profile: dict[str, Any]) -> str:
    selected = [label for key, label in INTERESTS if key in _selected(profile)]
    layouts = profile.get("layout_preferences", {})
    counts = profile.get("article_count_preferences", {})
    return (
        "Pulse status\n\n"
        f"Status: {profile.get('status', 'unknown')}\n"
        f"Selected interests: {len(selected)}\n"
        + ("\n".join(f"• {x}" for x in selected[:12]) or "• none yet")
        + "\n\n"
        f"Layouts: {json.dumps(layouts, sort_keys=True)}\n"
        f"Counts: {json.dumps(counts, sort_keys=True)}\n\n"
        "Briefing crons should remain paused until onboarding is complete and reviewed."
    )


async def handle_pulse_command(adapter: Any, msg: Any) -> None:
    profile = _profile()
    text = (
        "Pulse setup\n\n"
        "This is the real Telegram UI entry point. The old cron-only Pulse is paused until onboarding is complete.\n\n"
        "Start by selecting interest categories. Then choose layouts and article counts."
    )
    await adapter._bot.send_message(
        chat_id=msg.chat_id,
        text=text,
        reply_markup=_main_keyboard(profile),
    )


async def handle_pulse_callback(adapter: Any, query: Any, data: str) -> None:
    if re.fullmatch(r"pulse:(?:up|down|mute|explain):P[0-9A-Fa-f]{8}", data or ""):
        success, toast, followup = await dispatch_callback(data)
        await query.answer(text=toast)
        if success and followup:
            chat_id = getattr(getattr(query, "message", None), "chat_id", None)
            if chat_id is not None:
                await adapter.send(str(chat_id), followup)
        return

    profile = _profile()
    fb = _feedback()

    async def edit(text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await query.edit_message_text(text=text, reply_markup=markup)

    if data == "pulse:noop":
        await query.answer()
        return

    if data == "pulse:main":
        await query.answer("Pulse")
        await edit("Pulse menu", _main_keyboard(profile))
        return

    if data in {"pulse:onb:interests", "pulse:onboarding"}:
        if profile.get("status") == "onboarding_interview_pending" or profile.get("onboarding", {}).get("interview"):
            profile["status"] = "onboarding_interview_pending"
            _interview_state(profile)
            _save(PROFILE_PATH, profile)
            await query.answer("Interview")
            await edit(_interview_text(profile), _interview_keyboard(profile))
            return
        profile.setdefault("onboarding", {})["completed"] = False
        profile["status"] = "onboarding_required"
        _save(PROFILE_PATH, profile)
        await query.answer("Select interests")
        await edit("Pulse onboarding: choose everything you care about. You can tune priority later.", _interest_keyboard(profile))
        return

    if data == "pulse:onb:interview":
        profile["status"] = "onboarding_interview_pending"
        _interview_state(profile)
        _save(PROFILE_PATH, profile)
        await query.answer("Interview")
        await edit(_interview_text(profile), _interview_keyboard(profile))
        return

    if data.startswith("pulse:onb:q:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        state["current"] = min(max(idx, 0), max(len(questions) - 1, 0))
        state.pop("discussion", None)
        profile["status"] = "onboarding_interview_pending"
        _save(PROFILE_PATH, profile)
        await query.answer("Question")
        await edit(_interview_text(profile), _interview_keyboard(profile))
        return

    if data.startswith("pulse:onb:discuss:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        idx = min(max(idx, 0), max(len(questions) - 1, 0))
        state["current"] = idx
        old_discussion = state.get("discussion", {})
        state["discussion"] = {
            **old_discussion,
            "active": True,
            "question_index": idx,
            "chat_id": str(query.message.chat_id) if getattr(query, "message", None) else old_discussion.get("chat_id"),
            "started_at": old_discussion.get("started_at") or datetime.now(timezone.utc).isoformat(),
            "show_proposals": False if not old_discussion.get("pending_answer") else bool(old_discussion.get("show_proposals", True)),
        }
        profile["status"] = "onboarding_interview_pending"
        _note(fb, f"interview_discuss_start question_index={idx}")
        first_turn = not old_discussion.get("intro_sent_for_question") == idx
        if first_turn:
            state["discussion"]["intro_sent_for_question"] = idx
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Discussion started")
        await edit(_discussion_text(profile, idx), _discussion_keyboard(profile, idx))
        if first_turn:
            _enqueue_discussion_agent_turn(
                adapter,
                query,
                profile,
                idx,
                "Please start by explaining what this Pulse onboarding question is really deciding, compare the answer options in plain language, and ask me one useful follow-up question. Do not save a final answer.",
            )
        return

    if data.startswith("pulse:onb:type:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        idx = min(max(idx, 0), max(len(questions) - 1, 0))
        discussion = state.setdefault("discussion", {})
        discussion["active"] = True
        discussion["question_index"] = idx
        discussion["chat_id"] = str(query.message.chat_id) if getattr(query, "message", None) else discussion.get("chat_id")
        profile["status"] = "onboarding_interview_pending"
        _note(fb, f"interview_discuss_type_prompt question_index={idx}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Type your question in the message box", show_alert=True)
        await adapter._bot.send_message(
            chat_id=query.message.chat_id,
            text="Type your clarification question in the Telegram message box now. I’ll answer as the Hermes agent. Nothing you type here will be saved as the final Pulse answer until you explicitly confirm one.",
        )
        return

    if data.startswith("pulse:onb:writein:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        idx = min(max(idx, 0), max(len(questions) - 1, 0))
        discussion = state.setdefault("discussion", {})
        discussion["active"] = True
        discussion["question_index"] = idx
        discussion["chat_id"] = str(query.message.chat_id) if getattr(query, "message", None) else discussion.get("chat_id")
        discussion["awaiting_write_in"] = True
        discussion["show_proposals"] = True
        profile["status"] = "onboarding_interview_pending"
        _note(fb, f"interview_write_in_start question_index={idx}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Type your custom answer", show_alert=True)
        await adapter._bot.send_message(
            chat_id=query.message.chat_id,
            text="Type your custom final answer in the Telegram message box. I’ll show it back as a pending write-in, and it will only save after you tap ✅ Confirm final answer.",
        )
        return

    if data.startswith("pulse:onb:explain:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        idx = min(max(idx, 0), max(len(questions) - 1, 0))
        discussion = state.setdefault("discussion", {})
        discussion["active"] = True
        discussion["question_index"] = idx
        discussion["chat_id"] = str(query.message.chat_id) if getattr(query, "message", None) else discussion.get("chat_id")
        discussion["show_proposals"] = False
        profile["status"] = "onboarding_interview_pending"
        _note(fb, f"interview_discuss_explain question_index={idx}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Asking agent to explain")
        await edit(_discussion_text(profile, idx), _discussion_keyboard(profile, idx))
        _enqueue_discussion_agent_turn(
            adapter,
            query,
            profile,
            idx,
            "Explain and compare these answer options for me. Help me understand the tradeoffs. Ask one follow-up if the best choice depends on my intent. Do not save a final answer.",
        )
        return

    if data.startswith("pulse:onb:show_proposals:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad question")
            return
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        idx = min(max(idx, 0), max(len(questions) - 1, 0))
        discussion = state.setdefault("discussion", {})
        discussion["active"] = True
        discussion["question_index"] = idx
        discussion["show_proposals"] = True
        profile["status"] = "onboarding_interview_pending"
        _note(fb, f"interview_discuss_show_proposals question_index={idx}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Proposal buttons shown")
        await edit(_discussion_text(profile, idx), _discussion_keyboard(profile, idx))
        return

    if data.startswith("pulse:onb:propose:"):
        parts = data.split(":")
        if len(parts) != 5:
            await query.answer("Bad proposal")
            return
        try:
            idx = int(parts[3])
            opt_idx = int(parts[4])
        except ValueError:
            await query.answer("Bad proposal")
            return
        questions = _interview_questions(profile)
        if idx < 0 or idx >= len(questions):
            await query.answer("Unknown question")
            return
        q = questions[idx]
        if opt_idx < 0 or opt_idx >= len(q["options"]):
            await query.answer("Unknown option")
            return
        opt_key, opt_label = q["options"][opt_idx]
        state = _interview_state(profile)
        state["current"] = idx
        discussion = state.setdefault("discussion", {})
        discussion["active"] = True
        discussion["question_index"] = idx
        discussion["pending_answer"] = opt_key
        discussion["pending_answer_label"] = opt_label
        discussion["show_proposals"] = True
        discussion["pending_at"] = datetime.now(timezone.utc).isoformat()
        _note(fb, f"interview_discuss_propose question_index={idx} answer={opt_key}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Proposed, not saved")
        await edit(_discussion_text(profile, idx), _discussion_keyboard(profile, idx))
        return

    if data.startswith("pulse:onb:confirm_discuss:"):
        try:
            idx = int(data.rsplit(":", 1)[-1])
        except ValueError:
            await query.answer("Bad confirmation")
            return
        questions = _interview_questions(profile)
        if idx < 0 or idx >= len(questions):
            await query.answer("Unknown question")
            return
        q = questions[idx]
        state = _interview_state(profile)
        discussion = state.get("discussion", {})
        opt_key = discussion.get("pending_answer")
        if not opt_key:
            await query.answer("No proposed answer to confirm", show_alert=True)
            return
        allowed = {key for key, _label in q["options"]}
        custom_key = f"custom:{q['id']}"
        if opt_key not in allowed and opt_key != custom_key:
            await query.answer("Bad proposed answer", show_alert=True)
            return
        if opt_key == custom_key and not _custom_answer_label(profile, q["id"]):
            await query.answer("Missing custom answer text", show_alert=True)
            return
        state["current"] = idx
        values = _answer_values(profile, q["id"])
        if opt_key not in values:
            values.append(opt_key)
        _set_answer_values(profile, q["id"], values)
        discussion["active"] = False
        discussion["awaiting_write_in"] = False
        discussion["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        profile["status"] = "onboarding_interview_pending"
        profile.setdefault("interview_preferences", {})[q["id"]] = _answer_values(profile, q["id"])
        _note(fb, f"interview_discuss_confirm {q['id']}={opt_key}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Confirmed and saved")
        await edit(_interview_text(profile), _interview_keyboard(profile))
        return

    if data.startswith("pulse:onb:answer:"):
        parts = data.split(":")
        if len(parts) != 5:
            await query.answer("Bad answer")
            return
        try:
            idx = int(parts[3])
            opt_idx = int(parts[4])
        except ValueError:
            await query.answer("Bad answer")
            return
        questions = _interview_questions(profile)
        if idx < 0 or idx >= len(questions):
            await query.answer("Unknown question")
            return
        q = questions[idx]
        if opt_idx < 0 or opt_idx >= len(q["options"]):
            await query.answer("Unknown option")
            return
        opt_key, opt_label = q["options"][opt_idx]
        state = _interview_state(profile)
        state["current"] = idx
        selected = _toggle_answer_value(profile, q["id"], opt_key)
        profile["status"] = "onboarding_interview_pending"
        profile.setdefault("interview_preferences", {})[q["id"]] = _answer_values(profile, q["id"])
        _note(fb, f"interview_answer_toggle {q['id']}={opt_key} selected={selected} ({opt_label})")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Selected" if selected else "Removed")
        await edit(_interview_text(profile), _interview_keyboard(profile))
        return

    if data == "pulse:onb:complete_interview":
        questions = _interview_questions(profile)
        state = _interview_state(profile)
        missing = [q["id"] for q in questions if not _answer_values(profile, q["id"])]
        if missing:
            await query.answer(f"{len(missing)} unanswered question(s)", show_alert=True)
            return
        activation_result = _resume_pulse_jobs_for_activation(profile)
        resumed = bool(activation_result.get("resumed_jobs"))
        requested = set(_activation_values(profile))
        profile["status"] = "active" if resumed else "onboarding_review_pending"
        onboarding = profile.setdefault("onboarding", {})
        onboarding["interview_completed_at"] = datetime.now(timezone.utc).isoformat()
        onboarding["completed"] = resumed
        if resumed:
            onboarding["completed_at"] = onboarding.get("completed_at") or datetime.now(timezone.utc).isoformat()
            onboarding["activated_at"] = datetime.now(timezone.utc).isoformat()
        onboarding["required_before_resuming_crons"] = not resumed
        onboarding["activation_result"] = activation_result
        if "keep_paused" in requested or "review_first" in requested:
            onboarding["required_before_resuming_crons"] = True
        _note(fb, f"interview_complete activation={','.join(_activation_values(profile)) or 'none'} resumed={','.join(activation_result.get('resumed_jobs') or [])}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Interview complete")
        await edit(_completion_text(profile, activation_result), _main_keyboard(profile))
        return

    if data.startswith("pulse:onb:toggle:"):
        key = data.rsplit(":", 1)[-1]
        allowed = {k for k, _ in INTERESTS}
        if key not in allowed:
            await query.answer("Unknown interest")
            return
        selected = _selected(profile)
        if key in selected:
            selected.remove(key)
            label = "Removed"
        else:
            selected.add(key)
            label = "Selected"
        _set_selected(profile, selected)
        profile["status"] = "onboarding_required"
        _note(fb, f"interest_toggle {key} selected={key in selected}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer(label)
        await edit("Pulse onboarding: choose everything you care about. You can tune priority later.", _interest_keyboard(profile))
        return

    if data == "pulse:layouts":
        await query.answer("Layouts")
        await edit("Choose Pulse layouts by cadence.", _layout_keyboard(profile))
        return

    if data.startswith("pulse:layout:"):
        _, _, cadence, choice = data.split(":", 3)
        if cadence not in LAYOUTS or choice not in {k for k, _ in LAYOUTS[cadence]}:
            await query.answer("Unknown layout")
            return
        profile.setdefault("layout_preferences", {})[cadence] = choice
        _note(fb, f"layout {cadence}={choice}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Saved")
        await edit("Choose Pulse layouts by cadence.", _layout_keyboard(profile))
        return

    if data == "pulse:counts":
        await query.answer("Counts")
        await edit("Choose article counts. Custom counts can be added later by text once the UI baseline is working.", _count_keyboard(profile))
        return

    if data.startswith("pulse:count:"):
        _, _, cadence, raw = data.split(":", 3)
        try:
            count = int(raw)
        except ValueError:
            await query.answer("Bad count")
            return
        profile.setdefault("article_count_preferences", {})[cadence] = count
        _note(fb, f"count {cadence}={count}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Saved")
        await edit("Choose article counts. Custom counts can be added later by text once the UI baseline is working.", _count_keyboard(profile))
        return

    if data == "pulse:onb:finish":
        missing = []
        if not _selected(profile):
            missing.append("interests")
        if "daily" not in profile.get("layout_preferences", {}):
            missing.append("daily layout")
        if "daily" not in profile.get("article_count_preferences", {}):
            missing.append("daily article count")
        if missing:
            await query.answer("Missing: " + ", ".join(missing), show_alert=True)
            return
        profile["status"] = "onboarding_interview_pending"
        profile.setdefault("onboarding", {})["completed"] = False
        profile["onboarding"]["ui_selection_completed_at"] = datetime.now(timezone.utc).isoformat()
        _note(fb, "ui_selection_complete followup_interview_required")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Saved")
        await edit(
            "UI selections saved. Next step: Pulse should ask category-specific interview questions before crons resume. Status is onboarding_interview_pending.",
            _main_keyboard(profile),
        )
        return

    if data == "pulse:status":
        await query.answer("Status")
        await edit(_status_text(profile), _main_keyboard(profile))
        return

    if data == "pulse:pause":
        pause_result = _pause_pulse_jobs()
        profile["status"] = "onboarding_required"
        onboarding = profile.setdefault("onboarding", {})
        onboarding["required_before_resuming_crons"] = True
        onboarding["completed"] = False
        onboarding["pause_result"] = pause_result
        _note(fb, f"keep_crons_paused paused={','.join(pause_result.get('paused_jobs') or [])}")
        _save(PROFILE_PATH, profile)
        _save(FEEDBACK_PATH, fb)
        await query.answer("Pulse paused")
        text = "Pulse remains paused."
        if pause_result.get("paused_jobs"):
            text += " Paused jobs: " + ", ".join(pause_result["paused_jobs"]) + "."
        if pause_result.get("errors"):
            text += " Errors: " + "; ".join(pause_result["errors"])
        await edit(text, _main_keyboard(profile))
        return

    if data.startswith("pulse:item:"):
        # Future scheduled-brief integration target. The callback shape is:
        # pulse:item:<action>:<item_id>
        parts = data.split(":", 3)
        if len(parts) != 4:
            await query.answer("Bad Pulse item feedback")
            return
        _, _, action, item_id = parts
        fb.setdefault("item_feedback", {})[item_id.upper()] = {
            "action": action,
            "at": datetime.now(timezone.utc).isoformat(),
            "source": "telegram_ui",
        }
        _note(fb, f"item {action} {item_id.upper()}")
        _save(FEEDBACK_PATH, fb)
        await query.answer("Feedback saved")
        return

    await query.answer("Unknown Pulse action")
