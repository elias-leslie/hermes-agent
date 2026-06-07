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

        texts = [unit["text"].splitlines()[0] for unit in units]
        assert texts == [
            "Pulse Weekly",
            "### 1) Health emergency",
            "### 2) AI and semis",
            "## Process note",
        ]
        first_rows = units[1]["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in first_rows] == [
            "pulse:up:P6422755E",
            "pulse:up:P5D13601A",
        ]
        second_rows = units[2]["metadata"]["reply_markup"]["inline_keyboard"]
        assert [row[0]["callback_data"] for row in second_rows] == ["pulse:up:P00827930"]
        assert units[3]["metadata"] is None


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
