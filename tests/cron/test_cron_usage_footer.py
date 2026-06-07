from __future__ import annotations

import importlib


def _reload_scheduler(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.scheduler
    return importlib.reload(cron.scheduler)


def test_pulse_job_appends_usage_line_with_tokens_and_cost(tmp_path, monkeypatch):
    scheduler = _reload_scheduler(tmp_path, monkeypatch)

    result = {
        "api_calls": 3,
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 100,
        "cache_write_tokens": 50,
        "reasoning_tokens": 25,
        "total_tokens": 1500,
        "estimated_cost_usd": 0.01234,
        "cost_status": "estimated",
        "cost_source": "litellm",
    }

    final = scheduler._append_cron_usage_line_if_requested(
        {"name": "Pulse Daily", "skills": ["pulse-briefing-routine"]},
        "Brief body",
        result,
    )

    assert final.startswith("Brief body\n\nUsage: ")
    assert "1,500 tokens" in final
    assert "in 1,200" in final
    assert "out 300" in final
    assert "cache read 100" in final
    assert "cache write 50" in final
    assert "reasoning 25" in final
    assert "API calls 3" in final
    assert "~$0.0123" in final


def test_non_pulse_job_does_not_append_usage_line(tmp_path, monkeypatch):
    scheduler = _reload_scheduler(tmp_path, monkeypatch)

    final = scheduler._append_cron_usage_line_if_requested(
        {"name": "Weekly Health", "skills": []},
        "Health body",
        {"total_tokens": 99, "estimated_cost_usd": 1.0},
    )

    assert final == "Health body"


def test_pulse_usage_line_reports_included_and_unknown_costs(tmp_path, monkeypatch):
    scheduler = _reload_scheduler(tmp_path, monkeypatch)

    included = scheduler._append_cron_usage_line_if_requested(
        {"name": "Custom", "skills": ["pulse-briefing-routine"]},
        "Brief body",
        {"total_tokens": 42, "cost_status": "included", "estimated_cost_usd": None},
    )
    unknown = scheduler._append_cron_usage_line_if_requested(
        {"name": "Pulse Weekly"},
        "Brief body",
        {"total_tokens": 42, "cost_status": "unknown", "estimated_cost_usd": None},
    )

    assert "cost included" in included
    assert "cost n/a" in unknown
