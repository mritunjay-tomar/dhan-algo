"""Real Dhan reads; credentialed and opt-in, never submit an order."""
from dataclasses import replace
from unittest.mock import Mock
import pytest
from dhanhq import DhanContext, dhanhq
from dhan_algo.config import load_config
from dhan_algo.broker import DhanBroker
from dhan_algo.market import analyse_market
from dhan_algo.positions import dhan_positions
from dhan_algo.strategies.credit_spread import entry_check, exit_check

pytestmark = pytest.mark.integration


@pytest.fixture
def real_session(monkeypatch):
    # Force safe flags even if the shell is configured for live deployment.
    monkeypatch.setenv("DRY_RUN","true")
    monkeypatch.setenv("LIVE_TRADING_ENABLED","false")
    config=load_config()
    client=dhanhq(DhanContext(config.client_id,config.access_token))
    client.place_order=Mock(side_effect=AssertionError("Real orders are forbidden in tests"))
    broker=DhanBroker(client,config)
    yield config,broker,client
    client.place_order.assert_not_called()


def test_real_dhan_entry_and_exit_reads(real_session):
    config,broker,client=real_session
    positions=dhan_positions(broker)
    analysis=analyse_market(config)
    assert analysis.current_direction in {"BULLISH","BEARISH"}
    assert entry_check(config,broker,analysis=analysis)==0
    if positions:
        assert exit_check(config,broker,positions,analysis=analysis)==0
