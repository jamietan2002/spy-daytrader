"""Unit tests for closed-bar RTH normalization, SIP->IEX fallback, and pandas-ta indicators."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from alpaca.common.exceptions import APIError

from spy_daytrader.backtest import generate_synthetic_spy_5m_bars
from spy_daytrader.config import BotConfig
from spy_daytrader.data_feed import MarketDataFeed, normalize_and_filter_rth_closed_bars
from spy_daytrader.indicators import compute_indicators


def test_rth_and_closed_bar_filtering_drops_forming_and_extended_hours() -> None:
    # Construct bars in UTC on Wednesday 2026-10-14 (EDT = UTC-4):
    # 13:25 UTC = 09:25 ET (pre-market -> drop)
    # 13:30 UTC = 09:30 ET (closes 09:35 ET -> keep at 09:38 ET)
    # 13:35 UTC = 09:35 ET (closes 09:40 ET -> STILL FORMING at 09:38 ET -> drop!)
    ts_utc = pd.to_datetime(
        [
            "2026-10-14T13:25:00Z",
            "2026-10-14T13:30:00Z",
            "2026-10-14T13:35:00Z",
        ]
    )
    idx = pd.MultiIndex.from_arrays(
        [["SPY", "SPY", "SPY"], ts_utc], names=["symbol", "timestamp"]
    )
    raw = pd.DataFrame(
        {
            "open": [580.0, 580.5, 581.0],
            "high": [580.6, 581.2, 581.5],
            "low": [579.9, 580.2, 580.8],
            "close": [580.4, 581.0, 581.3],
            "volume": [10_000, 250_000, 120_000],
        },
        index=idx,
    )

    # At 13:38 UTC (09:38 ET), only the 09:30 ET bar has closed (09:35 <= 09:38)
    now_0938 = datetime(2026, 10, 14, 13, 38, 0, tzinfo=timezone.utc)
    closed_at_0938 = normalize_and_filter_rth_closed_bars(
        raw, symbol="SPY", bar_minutes=5, now_utc=now_0938
    )
    assert len(closed_at_0938) == 1
    assert closed_at_0938.index[0].hour == 9
    assert closed_at_0938.index[0].minute == 30

    # At 13:40:01 UTC (09:40:01 ET), the 09:35 ET bar is now closed and included
    now_0940 = datetime(2026, 10, 14, 13, 40, 1, tzinfo=timezone.utc)
    closed_at_0940 = normalize_and_filter_rth_closed_bars(
        raw, symbol="SPY", bar_minutes=5, now_utc=now_0940
    )
    assert len(closed_at_0940) == 2
    assert list(closed_at_0940.index.minute) == [30, 35]


def test_sip_entitlement_error_falls_back_to_iex() -> None:
    bars = generate_synthetic_spy_5m_bars(sessions=5)
    calls: list[str] = []

    class DummyClient:
        def get_stock_bars(self, req: object) -> object:
            feed_val = str(getattr(req, "feed", ""))
            calls.append(feed_val)
            if "sip" in feed_val.lower():
                raise APIError('{"message":"subscription does not permit querying recent SIP data"}')
            return SimpleNamespace(df=bars)

    cfg = BotConfig(alpaca_api_key="k", alpaca_secret_key="s", data_feed="sip")
    feed = MarketDataFeed(cfg, client=DummyClient())  # type: ignore[arg-type]
    out = feed.fetch_closed_bars(now_utc=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert len(calls) == 2
    assert not out.empty


def test_indicators_converge_and_session_vwap_resets_daily() -> None:
    cfg = BotConfig()
    bars = generate_synthetic_spy_5m_bars(sessions=12)  # 12 * 78 = 936 bars (> 650)
    enriched = compute_indicators(bars.tail(650), cfg)

    assert len(enriched) == 650
    tail = enriched.tail(50)
    for col in ("ema_200", "stochrsi_k", "stochrsi_d", "atr_14", "session_vwap"):
        assert not tail[col].isna().any(), f"Unexpected NaN in {col}"
        assert np.isfinite(tail[col]).all()

    # Verify session_vwap on the very first 09:30 ET bar of a day equals that bar's typical price
    opening_bars = enriched[enriched.index.time == pd.Timestamp("09:30").time()]
    assert len(opening_bars) >= 5
    sample_open = opening_bars.iloc[-1]
    expected_tp = (sample_open["high"] + sample_open["low"] + sample_open["close"]) / 3.0
    assert pytest.approx(sample_open["session_vwap"], rel=1e-6) == expected_tp

