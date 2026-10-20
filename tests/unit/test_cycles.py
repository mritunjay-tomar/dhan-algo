from dataclasses import replace
from unittest.mock import Mock
import itertools
import pytest
import strategy

execution = strategy


@pytest.mark.parametrize("direction", ["BULLISH","BEARISH"])
@pytest.mark.parametrize("dry,enabled", [(True,False),(True,True),(False,False)])
def test_entry_read_only_flags(config,client,analysis,chain,monkeypatch,direction,dry,enabled,caplog):
    http=Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain])
    monkeypatch.setattr(strategy,"dhan_request",http)
    cfg=replace(config,dry_run=dry,live_trading_enabled=enabled,nifty_quantity=2)
    with caplog.at_level("INFO"):
        assert strategy.entry_check(cfg,client,analysis=replace(analysis,current_direction=direction)) == 0
    assert '"quantity":100' not in caplog.text
    assert '"quantity":50' in caplog.text
    assert "DRY_RUN_ENTRY_SIGNAL" in caplog.text
    assert http.call_count==2
    client.place_order.assert_not_called()


@pytest.mark.parametrize("target,cross,expiry", list(itertools.product([False,True],repeat=3)))
def test_exit_reason_combinations(config,client,positions,analysis,chain,monkeypatch,caplog,target,cross,expiry):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-13","2026-10-20"]},chain]))
    monkeypatch.setattr(strategy,"spread_profit_target_reached",lambda *_: (target,{}))
    monkeypatch.setattr(strategy,"opposite_supertrend_cross",lambda *_:cross)
    monkeypatch.setattr(strategy,"bought_option_expiring_within_two_days",lambda *_:expiry)
    with caplog.at_level("INFO"):
        assert strategy.exit_check(config,client,positions,analysis=analysis)==0
    assert ("DRY_RUN_EXIT_SIGNAL" if any([target,cross,expiry]) else "NO_EXIT_SIGNAL") in caplog.text
    for flag,reason in [(target,"RUNNING_PROFIT_AT_OR_ABOVE_50_PERCENT_OF_MAXIMUM_PROFIT"),(cross,"OPPOSITE_30_MINUTE_SUPERTREND_CROSS"),(expiry,"LONG_OPTION_EXPIRY_WITHIN_TWO_DAYS")]:
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


@pytest.mark.parametrize("remaining", [True, False])
def test_reentry_requires_flat_account(config,client,positions,analysis,monkeypatch,remaining):
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    monkeypatch.setattr(strategy,"spread_profit_target_reached",lambda *_: (True,{}))
    monkeypatch.setattr(execution,"EXIT_ORDER_POLL_TIMEOUT_SECONDS",0)
    orders = [{"security_id": str(p["securityId"]), "quantity": 25,
               "response": {"status": "success", "data": {"orderId": str(i)}}}
              for i,p in enumerate(positions)]
    monkeypatch.setattr(strategy,"close_open_fno_positions",Mock(return_value=orders))
    client.get_positions.return_value={"data":positions if remaining else []}
    entry=Mock(return_value=0);monkeypatch.setattr(strategy,"entry_check",entry)
    assert strategy.exit_check(cfg,client,positions,analysis=analysis)==int(remaining)
    if not remaining: entry.assert_called_once_with(cfg,client,analysis=analysis,reentry=True)
    else: entry.assert_not_called()
    assert client.get_order_by_id.call_count == 2


def test_execution_branch_entry_with_fake_only(config,client,analysis,chain,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain]))
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.entry_check(cfg,client,analysis=analysis)==0
    assert client.place_order.call_count==2


def test_entry_rejected_order_returns_failure(config,client,analysis,chain,monkeypatch):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-06","2026-10-13"]},chain]))
    client.place_order.return_value={"status":"failure"}
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.entry_check(cfg,client,analysis=analysis)==1
    assert client.place_order.call_count==1


def test_exit_submission_failure_blocks_reentry(config,client,positions,analysis,monkeypatch):
    monkeypatch.setattr(strategy,"spread_profit_target_reached",lambda *_: (True,{}))
    entry=Mock();monkeypatch.setattr(strategy,"entry_check",entry)
    client.place_order.side_effect=ValueError("fake rejection")
    cfg=replace(config,dry_run=False,live_trading_enabled=True)
    assert strategy.exit_check(cfg,client,positions,analysis=analysis)==1
    entry.assert_not_called()


@pytest.mark.parametrize("dry,enabled", [(True,False),(True,True),(False,False)])
def test_actual_exit_target_read_only(config,client,positions,analysis,chain,monkeypatch,dry,enabled,caplog):
    monkeypatch.setattr(strategy,"dhan_request",Mock(side_effect=[{"data":["2026-10-13","2026-10-20"]},chain]))
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"]=76
    cfg=replace(config,dry_run=dry,live_trading_enabled=enabled)
    with caplog.at_level("INFO"):
        assert strategy.exit_check(cfg,client,positions,analysis=analysis)==0
    assert "DRY_RUN_EXIT_SIGNAL" in caplog.text
    client.place_order.assert_not_called()
