"""Real Dhan reads; credentialed and opt-in, never submit an order."""
from dataclasses import replace
from unittest.mock import Mock
import pytest
from dhanhq import DhanContext, dhanhq
from strategy import analyse_market, dhan_positions, entry_check, exit_check, load_config

pytestmark = pytest.mark.integration


@pytest.fixture
def real_session(monkeypatch):
    # Force safe flags even if the shell is configured for live deployment.
    monkeypatch.setenv("DRY_RUN","true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED","false")
    config=load_config()
    client=dhanhq(DhanContext(config.client_id,config.access_token))
    client.place_order=Mock(side_effect=AssertionError("Real orders are forbidden in tests"))
    yield config,client
    client.place_order.assert_not_called()


def test_real_dhan_entry_and_exit_reads(real_session):
    config,client=real_session
    positions=dhan_positions(client)
    analysis=analyse_market(config)
    assert analysis.current_direction in {"BULLISH","BEARISH"}
    assert entry_check(config,client,analysis=analysis)==0
    if positions:
        assert exit_check(config,client,positions,analysis=analysis)==0
