import json
from dataclasses import replace
from unittest.mock import Mock
import pytest
from dhan_algo.strategies.base import Strategy
from dhan_algo.strategies.credit_spread import CreditSpreadStrategy
from dhan_algo.runner import load_strategies, run_strategies, validate_portfolio
from dhan_algo.broker import DhanBroker


class Probe(Strategy):
    def run_cycle(self, broker):
        self.seen=broker.get_positions()["data"]
        return 0


def test_abstract_strategy(config):
    with pytest.raises(TypeError): Strategy("abstract",config)


def test_multiple_strategies_see_only_own_positions(config,client,positions):
    client.get_positions.return_value={"data":positions}
    strategies=[Probe("first",replace(config,security_ids=frozenset({"101"}))),
                Probe("second",replace(config,security_ids=frozenset({"102"})))]
    assert run_strategies(client,strategies)==0
    assert [s.seen for s in strategies]==[[positions[0]],[positions[1]]]
    client.place_order.assert_not_called()


@pytest.mark.parametrize("scopes", [(None,None),({"101"},{"101"}),(set(),{"102"})])
def test_ambiguous_ownership_rejected(config,client,scopes):
    strategies=[Probe(str(i),replace(config,security_ids=None if s is None else frozenset(s))) for i,s in enumerate(scopes)]
    with pytest.raises(ValueError): run_strategies(client,strategies)
    client.get_positions.assert_not_called()


def test_failure_does_not_prevent_next_strategy(config,client):
    first=Probe("first",replace(config,security_ids=frozenset({"101"})))
    second=Probe("second",replace(config,security_ids=frozenset({"102"})))
    first.run_cycle=Mock(side_effect=ValueError("fixture failure"))
    assert run_strategies(client,[first,second])==1
    assert second.seen==[]


def test_duplicate_name_and_empty_portfolio(config):
    for portfolio in [[],[Probe("same",config),Probe("same",config)]]:
        with pytest.raises(ValueError): validate_portfolio(portfolio)


def test_scoped_broker_rejects_outside_order(config,client):
    broker=DhanBroker(client,replace(config,security_ids=frozenset({"101"}),dry_run=False,live_trading_enabled=True))
    with pytest.raises(ValueError): broker.place_order(security_id="102",exchange_segment="NSE_FNO")
    client.place_order.assert_not_called()
    broker.place_order(security_id="101",exchange_segment="NSE_FNO")
    client.place_order.assert_called_once()


def test_load_multiple_instances(config,tmp_path):
    path=tmp_path/"strategies.json"
    path.write_text(json.dumps([dict(name="first",type="credit_spread",security_ids=[101,102]),
        dict(name="second",type="credit_spread",security_ids=[201,203],parameters={"supertrend_atr_period":10})]))
    strategies=load_strategies(path,config)
    assert len(strategies)==2
    assert isinstance(strategies[0],CreditSpreadStrategy)
    assert strategies[1].config.supertrend_atr_period==10


@pytest.mark.parametrize("entry", [dict(name="x",type="unknown"),dict(name="x",type="credit_spread",parameters={"dry_run":False}),
    dict(name="x",type="credit_spread",security_ids=[]),dict(name="x",type="credit_spread",security_ids="101")])
def test_invalid_registry_config(config,tmp_path,entry):
    path=tmp_path/"strategies.json";path.write_text(json.dumps([entry]))
    with pytest.raises(ValueError):load_strategies(path,config)
