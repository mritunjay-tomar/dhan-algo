from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import pytest
from dhan_algo import config as settings
from dhan_algo import market


@pytest.mark.parametrize("field,value", [("nifty_quantity",0),("nifty_quantity",True),("nifty_lot_size",-1),
    ("request_timeout_seconds",0),("request_timeout_seconds",float("nan")),("supertrend_multiplier",0),
    ("supertrend_atr_period",0),("supertrend_lookback_days",0),("access_token","")])
def test_config_validation(config,field,value):
    with pytest.raises(ValueError): replace(config,**{field:value}).validate()


def test_token_not_in_config_repr(config):
    assert "test-token" not in repr(config)


def test_existing_token_avoids_auth(monkeypatch):
    monkeypatch.setenv("CLIENT_ID","test");monkeypatch.setenv("DHAN_ACCESS_TOKEN","token")
    monkeypatch.setenv("NIFTY_LOT_SIZE","25");monkeypatch.setenv("DRY_RUN","true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED","false")
    generate=Mock(side_effect=AssertionError("Should not generate token"))
    monkeypatch.setattr(settings,"generate_access_token",generate)
    assert settings.load_config().dry_run
    generate.assert_not_called()


@pytest.mark.parametrize("value", ["yes","1","", "{{ FLAG }}"])
def test_strict_boolean(monkeypatch,value):
    monkeypatch.setenv("FLAG",value)
    with pytest.raises(ValueError): settings.env_bool("FLAG",True)


def candle(hour,minute,close=100):
    return dict(open=99.,high=105.,low=95.,close=float(close),
                timestamp=datetime(2020,1,2,hour,minute,tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())


def test_resample_aligned_complete_pairs_only():
    rows=[candle(9,30,102),candle(9,15),candle(9,45)]
    result=market.resample_15m_to_30m(rows)
    assert len(result)==1
    assert result[0]=={**candle(9,15),"close":102.}


def test_resample_rejects_duplicate_candle():
    assert market.resample_15m_to_30m([candle(9,15),candle(9,15)])==[]


def test_future_candles_excluded():
    future={**candle(9,15),"timestamp":datetime(2099,1,1,tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()}
    assert market.completed_candles([future],15)==[]


@pytest.mark.parametrize("data", [{},{"open":[],"high":[],"low":[],"close":[1],"timestamp":[]},
    {"open":[1],"high":[1],"low":[1],"close":[1]}])
def test_bad_candles_rejected(data):
    with pytest.raises(ValueError):market.candle_rows(data)


def test_real_supertrend_calculation():
    rows=[dict(open=100+i,high=103+i,low=98+i,close=101+i,timestamp=i*900) for i in range(100)]
    values=market.supertrend(rows,22,4.)
    assert len(values)==100
    assert values[-1] is not None
    assert market.current_direction(rows,values)=="BULLISH"


def test_insufficient_candles():
    with pytest.raises(ValueError):market.supertrend([],22,4.)
