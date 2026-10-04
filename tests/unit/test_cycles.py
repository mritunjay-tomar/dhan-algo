from dataclasses import replace
from unittest.mock import Mock
import itertools
import pytest
from dhan_algo.broker import DhanBroker
from dhan_algo.strategies import credit_spread as strategy


@pytest.mark.parametrize("direction", ["BULLISH","BEARISH"])
@pytest.mark.parametrize("dry,enabled", [(True,False),(True,True),(False,False)])
def test_entry_read_only_flags(config,client,analysis,chain,monkeypatch,direction,dry,enabled,caplog):
    http=Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain])
    monkeypatch.setattr(strategy,"dhan_request",http)
    cfg=replace(config,dry_run=dry,live_trading_enabled=enabled,nifty_quantity=2)
    with caplog.at_level("INFO"):
        assert strategy.entry_check(cfg,DhanBroker(client,cfg),analysis=replace(analysis,current_direction=direction)) == 0
    assert '"quantity":100' not in caplog.text
    assert '"quantity":50' in caplog.text
    assert "DRY_RUN_ENTRY_SIGNAL" in caplog.text
    assert http.call_count==2
    client.place_order.assert_not_called()


@pytest.mark.parametrize("target,cross,expiry", list(itertools.product([False,True],repeat=3)))
def test_exit_reason_combinations(config,client,positions,analysis,monkeypatch,caplog,target,cross,expiry):
    monkeypatch.setattr(strategy,"short_legs_at_half_price",lambda *_:(target,[]))
    monkeypatch.setattr(strategy,"opposite_supertrend_cross",lambda *_:cross)
    monkeypatch.setattr(strategy,"bought_option_expiring_within_two_days",lambda *_:expiry)
    with caplog.at_level("INFO"):
        assert strategy.exit_check(config,DhanBroker(client,config),positions,analysis=analysis)==0
    assert ("DRY_RUN_EXIT_SIGNAL" if any([target,cross,expiry]) else "NO_EXIT_SIGNAL") in caplog.text
    for flag,reason in [(target,"ALL_SHORT_LEGS_AT_OR_BELOW_HALF_PRICE"),(cross,"OPPOSITE_30_MINUTE_SUPERTREND_CROSS"),(expiry,"LONG_OPTION_EXPIRY_WITHIN_TWO_DAYS")]:
        assert (reason in caplog.text) is flag
    client.place_order.assert_not_called()


def test_entry_invalid_data_returns_failure(config,client,analysis,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",lambda *_:{"data":[]})
    assert strategy.entry_check(config,client,analysis=analysis)==1
    client.place_order.assert_not_called()


@pytest.mark.parametrize("active", [True,False])
def test_cycle_shares_one_analysis(config,client,positions,analysis,monkeypatch,active):
    market=Mock(return_value=analysis); entry=Mock(return_value=0); exit_=Mock(return_value=0)
    monkeypatch.setattr(strategy,"analyse_market",market)
    monkeypatch.setattr(strategy,"entry_check",entry)
    monkeypatch.setattr(strategy,"exit_check",exit_)
    assert strategy.run_strategy(config,client,positions=positions if active else [])==0
    market.assert_called_once_with(config)
    assert (exit_ if active else entry).call_args.kwargs["analysis"] is analysis
    (entry if active else exit_).assert_not_called()


@pytest.mark.parametrize("filled,remaining,result", [(False,False,1),(True,True,1),(True,False,0)])
def test_reentry_requires_confirmed_flat_account(config,client,positions,analysis,monkeypatch,filled,remaining,result):
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    monkeypatch.setattr(strategy,"short_legs_at_half_price",lambda *_:(True,[]))
    monkeypatch.setattr(strategy,"close_open_fno_positions",Mock(return_value=[{}]))
    monkeypatch.setattr(strategy,"wait_for_exit_orders_traded",Mock(return_value=(filled,[])))
    client.get_positions.return_value={"data":positions if remaining else []}
    entry=Mock(return_value=0);monkeypatch.setattr(strategy,"entry_check",entry)
    assert strategy.exit_check(cfg,client,positions,analysis=analysis)==result
    if result==0: entry.assert_called_once_with(cfg,client,analysis=analysis,reentry=True)
    else: entry.assert_not_called()


def test_scope_filters_entry_contracts(config,client,analysis,chain,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain]))
    assert strategy.entry_check(replace(config,security_ids=frozenset({"201","203"})),client,analysis=analysis)==1
    client.place_order.assert_not_called()


def test_execution_branch_entry_with_fake_only(config,client,analysis,chain,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain]))
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.entry_check(cfg,DhanBroker(client,cfg),analysis=analysis)==0
    assert client.place_order.call_count==2


def test_entry_rejected_order_returns_failure(config,client,analysis,chain,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain]))
    client.place_order.return_value={"status":"failure"}
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.entry_check(cfg,DhanBroker(client,cfg),analysis=analysis)==1
    assert client.place_order.call_count==1


def test_exit_submission_failure_blocks_reentry(config,client,positions,analysis,monkeypatch):
    monkeypatch.setattr(strategy,"short_legs_at_half_price",lambda *_:(True,[]))
    entry=Mock();monkeypatch.setattr(strategy,"entry_check",entry)
    client.place_order.side_effect=ValueError("fake rejection")
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.exit_check(cfg,DhanBroker(client,cfg),positions,analysis=analysis)==1
    entry.assert_not_called()


@pytest.mark.parametrize("dry,enabled", [(True,False),(True,True),(False,False)])
def test_actual_exit_target_read_only(config,client,positions,analysis,dry,enabled,caplog):
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"]=50
    cfg=replace(config,dry_run=dry,live_trading_enabled=enabled)
    with caplog.at_level("INFO"):
        assert strategy.exit_check(cfg,DhanBroker(client,cfg),positions,analysis=analysis)==0
    assert "DRY_RUN_EXIT_SIGNAL" in caplog.text
    client.place_order.assert_not_called()
