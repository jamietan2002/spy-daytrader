"""Unit tests for paper-trading safety locks, config validation, and secret redaction."""

from __future__ import annotations

import logging
import pytest

from spy_daytrader.config import BotConfig, LiveTradingProhibitedError, StrategyMode
from spy_daytrader.logging_utils import SecretScrubbingFilter


def test_paper_mode_lock_blocks_false_flag() -> None:
    with pytest.raises(LiveTradingProhibitedError):
        BotConfig(alpaca_paper=False)


def test_paper_mode_lock_blocks_live_url() -> None:
    with pytest.raises(LiveTradingProhibitedError):
        BotConfig(alpaca_paper=True, alpaca_base_url="https://api.alpaca.markets")


def test_env_blocks_live_paper_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(LiveTradingProhibitedError):
        BotConfig.from_env()


def test_bars_to_fetch_rejects_insufficient_warmup_direct() -> None:
    with pytest.raises(ValueError, match="bars_to_fetch"):
        BotConfig(bars_to_fetch=100, ema_length=200)


def test_from_env_auto_heals_100_bars_for_200_ema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
    monkeypatch.setenv("BARS_TO_FETCH", "100")
    monkeypatch.setenv("EMA_LENGTH", "200")
    cfg = BotConfig.from_env(mode_override=StrategyMode.BASELINE)
    assert cfg.bars_to_fetch >= 300
    assert cfg.strategy_mode == StrategyMode.BASELINE
    assert cfg.allow_short is False


def test_secret_scrubbing_filter_redacts_keys() -> None:
    filt = SecretScrubbingFilter(explicit_secrets=["mySuperSecretValue999"])
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Key=PKABCDEF1234567890XYZ token=mySuperSecretValue999 auth=Bearer eyJhbGciOiJIUzI1NiJ9",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "PKABCDEF1234567890XYZ" not in record.msg
    assert "mySuperSecretValue999" not in record.msg
    assert "eyJhbGciOiJIUzI1NiJ9" not in record.msg
    assert "[REDACTED]" in record.msg

