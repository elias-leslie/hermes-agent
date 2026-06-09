"""Tests for Pulse Telegram inline-button support."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    class FakeButton:
        def __init__(self, text, callback_data=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url

    class FakeMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    mod = MagicMock()
    mod.InlineKeyboardButton = FakeButton
    mod.InlineKeyboardMarkup = FakeMarkup
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from gateway.platforms import pulse_ui
from gateway.platforms.telegram import TelegramAdapter


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestPulseUi:
    def test_build_reply_markup_spec_from_item_ids(self):
        spec = pulse_ui.build_reply_markup_spec("1. P05E8F791 thing\n2. P00969B9E other")

        assert spec is not None
        rows = spec["inline_keyboard"]
        assert len(rows) == 2
        assert rows[0][0]["callback_data"] == "pulse:up:P05E8F791"
        assert rows[0][1]["callback_data"] == "pulse:down:P05E8F791"
        assert rows[0][2]["callback_data"] == "pulse:mute:P05E8F791"
        assert rows[0][3]["callback_data"] == "pulse:explain:P05E8F791"

    def test_extract_item_ids_dedupes_and_caps(self):
        text = "P00000001 P00000001 " + " ".join(f"P{i:08X}" for i in range(2, 30))
        ids = pulse_ui.extract_item_ids(text, limit=3)

        assert ids == ["P00000001", "P00000002", "P00000003"]

    def test_split_brief_for_delivery_attaches_buttons_per_item(self):
        units = pulse_ui.split_brief_for_delivery(
            "Pulse Daily\n\n"
            "1) First item. ID: P05E8F791\n\n"
            "2) Second item. IDs: P00969B9E / P00000003\n\n"
            "Feedback: reply with IDs if buttons are unavailable."
        )

        assert [unit["text"].splitlines()[0] for unit in units] == [
            "Pulse Daily",
            "1) First item. ID: P05E8F791",
            "2) Second item. IDs: P00969B9E / P00000003",
            "Feedback: reply with IDs if buttons are unavailable.",
        ]
        assert units[0]["metadata"] is None
        assert units[1]["metadata"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pulse:up:P05E8F791"
        second_rows = units[2]["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in second_rows] == [
            "pulse:up:P00969B9E",
            "pulse:up:P00000003",
        ]
        assert units[3]["metadata"] is None

    def test_split_brief_for_delivery_attaches_buttons_to_weekly_sections(self):
        units = pulse_ui.split_brief_for_delivery(
            "Pulse Weekly\n\n"
            "### 1) Health emergency\n\n"
            "Weekly synthesis paragraph.\n\n"
            "IDs: `P6422755E`, `P5D13601A`\n\n"
            "---\n\n"
            "### 2) AI and semis\n\n"
            "Another synthesis paragraph.\n\n"
            "IDs: `P00827930`\n\n"
            "---\n\n"
            "## Process note\nNo item IDs here."
        )

        # The new splitter (Strategy 2 — inline ID) catches the IDs line in
        # each section and attaches per-ID buttons. Sections without IDs
        # (intro, body paragraphs) are plain messages. This is better UX
        # than the prior single-keyboard-per-section approach because the
        # user can see which item the thumbs up/down applies to.
        buttoned_units = [u for u in units if u.get("metadata")]
        assert len(buttoned_units) == 2
        first_rows = buttoned_units[0]["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in first_rows] == [
            "pulse:up:P6422755E",
            "pulse:up:P5D13601A",
        ]
        second_rows = buttoned_units[1]["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in second_rows] == ["pulse:up:P00827930"]

    def test_split_brief_for_delivery_handles_inline_bold_id_items(self):
        # The current Pulse agents emit items as `**PID** (Source) — sentence`.
        # This is the shape that previously lost per-item buttons because the
        # strict numbered/bulleted regex did not match. The new splitter
        # (Strategy 2) must attach per-item buttons to each of these.
        units = pulse_ui.split_brief_for_delivery(
            "Pulse Daily — Tue 2026-06-09\n\n"
            "Pre-market. Quotes: VTI $366.40.\n\n"
            "**🌍 Geopolitics & markets**\n\n"
            "- **P5E0FCF4E** (BBC) — Asia tech sell-off; oil volatile. Impact: VGT bearish near-term.\n"
            "- **P24B44675** (Al Jazeera) — Why oil held near $100/bbl. Impact: indirect inflation pressure.\n"
            "- **PE00A60AA** (SCMP) — Trump urges Iran/Israel to stop shooting. Risk-on if holds.\n"
            "\n"
            "**🛡️ Cybersecurity**\n\n"
            "- **P2D12CF75** (CISA) — Two new KEVs: LiteLLM command injection, Check Point auth bypass.\n"
            "- **P82E52F76** (arXiv) — Zero-shot embedding drift detection for prompt-injection defense.\n"
            "\n"
            "Feedback: `Pulse up <ID>` / `Pulse down <ID>`."
        )

        buttoned = [u for u in units if u.get("metadata")]
        assert len(buttoned) >= 5
        # Each buttoned unit should have buttons for the ID it contains.
        first_callbacks = [row[0]["callback_data"] for row in buttoned[0]["metadata"]["reply_markup"]["inline_keyboard"]]
        assert first_callbacks == ["pulse:up:P5E0FCF4E"]
        # Check the CISA item (the one the user complained about — explain button).
        cisa_unit = next(u for u in buttoned if "P2D12CF75" in u["text"])
        cisa_callbacks = [row[i]["callback_data"] for row in cisa_unit["metadata"]["reply_markup"]["inline_keyboard"] for i in range(4)]
        assert "pulse:up:P2D12CF75" in cisa_callbacks
        assert "pulse:down:P2D12CF75" in cisa_callbacks
        assert "pulse:mute:P2D12CF75" in cisa_callbacks
        assert "pulse:explain:P2D12CF75" in cisa_callbacks

    def test_split_brief_for_delivery_handles_market_mode_bold_headers(self):
        # Premarket / EOD / Afterhours briefs use bold-header thematic blocks
        # with `IDs:` lines. The heading-section splitter (Strategy 3) should
        # split these into per-section units with per-section buttons.
        units = pulse_ui.split_brief_for_delivery(
            "**Pulse Market Premarket — 2026-06-09**\n\n"
            "Session header line.\n\n"
            "**🌍 Overnight headlines**\n\n"
            "Ebola outbreak in DRC spreading at unprecedented rate.\n"
            "DRC officials and NPR flag accelerating transmission.\n"
            "IDs: P0BB09EDB PA7314C30\n\n"
            "---\n\n"
            "**Position impact**\n\n"
            "Held: VTI ~61%, VGT ~5%.\n"
            "Directional bias: mild-to-moderately negative at the open.\n\n"
            "---\n\n"
            "**Other items on the radar**\n\n"
            "SIPRI yearbook: China added ~20 warheads.\n"
            "IDs: P3EB28592 P621FA4B3\n"
        )

        buttoned = [u for u in units if u.get("metadata")]
        assert len(buttoned) >= 2
        # The first thematic block has two IDs and should have two button rows.
        first_block = next(u for u in buttoned if "P0BB09EDB" in u["text"])
        rows = first_block["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in rows] == [
            "pulse:up:P0BB09EDB",
            "pulse:up:PA7314C30",
        ]
        # The radar block has its own IDs.
        radar_block = next(u for u in buttoned if "P3EB28592" in u["text"])
        radar_rows = radar_block["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in radar_rows] == [
            "pulse:up:P3EB28592",
            "pulse:up:P621FA4B3",
        ]

    def test_split_brief_for_delivery_falls_back_to_full_keyboard_when_few_items(self):
        # When the brief has at most 1 ID, fall back to a single message with
        # a single keyboard so the user still gets controls.
        units = pulse_ui.split_brief_for_delivery(
            "Pulse Daily — placeholder\n\nThis brief has no items yet."
        )
        assert len(units) == 1
        assert units[0]["metadata"] is None

        units = pulse_ui.split_brief_for_delivery(
            "Pulse Daily — one item\n\n**PABC12345** (BBC) — single-item brief."
        )
        assert len(units) == 1
        assert units[0]["metadata"] is not None
        assert units[0]["metadata"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "pulse:up:PABC12345"

    def test_split_brief_for_delivery_preserves_session_header_in_premarket(self):
        # The Premarket / EOD / Afterhours briefs use the shape:
        #   **<Session header — date>**
        #   <body>
        #   ---
        #   **<Themed block header>**
        #   <body>
        #   IDs: PIDs
        #   ---
        #   **<Next themed block header>**
        #   <body>
        #   IDs: PIDs
        # Each themed block should land in its own delivery unit WITH its
        # block header at the top, so the user sees header + body + IDs +
        # buttons. Horizontal rules are dropped.
        units = pulse_ui.split_brief_for_delivery(
            "**Pulse Market Premarket — 2026-06-09**\n\n"
            "*Ebola outbreak in DRC.*\n"
            "DRC officials and NPR flag accelerating transmission.\n"
            "IDs: P0BB09EDB PA7314C30\n\n"
            "---\n\n"
            "**Other items on the radar**\n\n"
            "SIPRI yearbook: China added ~20 warheads.\n"
            "IDs: P3EB28592 P621FA4B3\n"
        )

        # Two buttoned units (one per themed block) plus the session header
        # which lives in the first unit (no separate plain unit needed).
        buttoned = [u for u in units if u.get("metadata")]
        assert len(buttoned) == 2
        # First block: session header is included at the top.
        first_block = buttoned[0]
        first_lines = first_block["text"].splitlines()
        assert first_lines[0] == "**Pulse Market Premarket — 2026-06-09**"
        assert "IDs: P0BB09EDB PA7314C30" in first_block["text"]
        # Second block: section header at the top, no horizontal rule in text.
        second_block = buttoned[1]
        second_lines = second_block["text"].splitlines()
        assert second_lines[0] == "**Other items on the radar**"
        assert "IDs: P3EB28592 P621FA4B3" in second_block["text"]
        # No horizontal rule should survive in any unit text.
        for u in units:
            assert "---" not in u["text"], f"unit still has a horizontal rule: {u['text'][:80]!r}"

    def test_split_brief_for_delivery_drops_orphan_horizontal_rules(self):
        # A brief that ends with a horizontal rule (e.g. trailing ---) should
        # not produce a trailing empty Telegram message.
        units = pulse_ui.split_brief_for_delivery(
            "Pulse Daily\n\n"
            "**PABC12345** (BBC) — single item.\n\n"
            "---"
        )
        # We expect at least the item, never an empty trailing unit.
        assert all(u["text"].strip() for u in units)
        for u in units:
            assert not u["text"].strip().startswith("---")

    def test_explain_followup_does_real_investigation(self, monkeypatch, tmp_path):
        # The explain button should curl the source URL and append a real
        # excerpt, not just dump local state. We monkeypatch subprocess.run
        # to return canned HTML, and assert the followup contains a clean
        # excerpt of that HTML (tags stripped, sentences preserved).
        from unittest.mock import MagicMock

        fake_curl = MagicMock()
        fake_curl.return_value = MagicMock(
            returncode=0,
            stdout=(
                "<html><head><title>x</title></head><body>"
                "<script>alert(1)</script>"
                "<p>CISA added two KEV vulnerabilities affecting LiteLLM and "
                "Check Point Security Gateway. Active exploitation confirmed. "
                "LiteLLM is widely deployed in agentic AI stacks; Check Point "
                "exposure depends on appliance configuration.</p>"
                "</body></html>"
            ),
        )
        fake_portfolio = MagicMock()
        fake_portfolio.return_value = MagicMock(
            returncode=0,
            stdout="VTI 92.66% / VGT 7.34%; tech lookthrough 38%; beta 1.07.",
        )
        monkeypatch.setattr(pulse_ui.subprocess, "run", lambda *a, **kw: (
            fake_portfolio.return_value if a and a[0] and a[0][0] == "st" else fake_curl.return_value
        ))

        item = {
            "id": "P2D12CF75",
            "title": "CISA Adds Two Known Exploited Vulnerabilities",
            "source": "CISA Cybersecurity Advisories",
            "url": "https://www.cisa.gov/news-events/cybersecurity-advisories",
            "topics": ["cybersecurity", "emergency"],
            "summary_hint": "Two new KEVs.",
        }
        monkeypatch.setitem(
            {"a": 1}, "a", 1
        )  # placeholder to keep monkeypatch import live
        # Inject the item into a fake state file path.
        import json as _json
        state_file = tmp_path / "state.json"
        state_file.write_text(_json.dumps({"recent_items": [item]}))
        monkeypatch.setattr(pulse_ui, "STATE_PATH", state_file)

        out = pulse_ui._format_explain_followup("P2D12CF75")
        assert "🔍 Pulse explain P2D12CF75" in out
        assert "CISA added two KEV" in out  # Real excerpt, not just summary_hint
        assert "<script>" not in out  # Tags stripped
        assert "<p>" not in out
        # Topics include cybersecurity → portfolio-adjacent, so the
        # held-position read should be in the response.
        assert "Held-position read" in out
        assert "VTI" in out

    def test_explain_followup_handles_missing_url_gracefully(self, monkeypatch, tmp_path):
        # When the item has no URL, the explain followup should still
        # produce a useful response from local state.
        import json as _json
        item = {
            "id": "PABC12345",
            "title": "Test item",
            "source": "Test source",
            "topics": ["ai"],
            "summary_hint": "Test summary hint for explain.",
        }
        state_file = tmp_path / "state.json"
        state_file.write_text(_json.dumps({"recent_items": [item]}))
        monkeypatch.setattr(pulse_ui, "STATE_PATH", state_file)

        out = pulse_ui._format_explain_followup("PABC12345")
        assert "Test summary hint" in out
        # Topics are "ai" only — not portfolio-adjacent, no portfolio block.
        assert "Held-position read" not in out


class TestTelegramPulseMarkup:
    @pytest.mark.asyncio
    async def test_send_passes_reply_markup_from_metadata(self):
        adapter = _make_adapter()
        assert adapter._bot is not None
        adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=123))
        metadata = {
            "reply_markup": {
                "inline_keyboard": [[{"text": "👍 P05E8F791", "callback_data": "pulse:up:P05E8F791"}]]
            }
        }

        class FakeButton:
            def __init__(self, text, callback_data=None, url=None):
                self.text = text
                self.callback_data = callback_data
                self.url = url

        class FakeMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

        with patch("gateway.platforms.telegram.InlineKeyboardButton", FakeButton), \
             patch("gateway.platforms.telegram.InlineKeyboardMarkup", FakeMarkup):
            result = await adapter.send("12345", "Pulse item P05E8F791", metadata=metadata)

        assert result.success is True
        kwargs = adapter._bot.send_message.call_args.kwargs
        assert "reply_markup" in kwargs
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "pulse:up:P05E8F791"

    @pytest.mark.asyncio
    async def test_callback_dispatches_pulse_prefix(self):
        adapter = _make_adapter()
        query = AsyncMock()
        query.data = "pulse:up:P05E8F791"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat = MagicMock(type="private")
        query.message.message_thread_id = None
        query.from_user = MagicMock(id="777", first_name="Tester")
        query.answer = AsyncMock()
        update = MagicMock(callback_query=query)

        with patch.object(adapter, "_is_callback_user_authorized", return_value=True), \
             patch("gateway.platforms.pulse_ui.dispatch_callback", AsyncMock(return_value=(True, "Saved", None))) as dispatch:
            await adapter._handle_callback_query(update, MagicMock())

        dispatch.assert_awaited_once_with("pulse:up:P05E8F791")
        query.answer.assert_awaited_once_with(text="Saved")
