from dataclasses import replace
from unittest.mock import Mock

import pytest
import strategy


def cheaper_chain(chain, direction):
    side = 'pe' if direction == 'BULLISH' else 'ce'
    strike = '24800' if direction == 'BULLISH' else '25200'
    chain['data']['oc'][strike] = {side: {
        'security_id': 'cheap-hedge', 'greeks': {'delta': -.1 if side == 'pe' else .1},
        'last_price': 25, 'top_bid_price': 24, 'top_ask_price': 26,
    }}
    return chain


@pytest.mark.parametrize('direction', ['BULLISH', 'BEARISH'])
def test_original_selection_when_funded(client, chain, direction):
    sell, buy, funding = strategy.affordable_option_legs(client, chain, direction, 25)
    assert (sell, buy) == strategy.option_legs(chain, direction)
    assert funding['required_funds'] == 11838.75
    payload = client.dhan_http.post.call_args.args[1]
    assert [s['transactionType'] for s in payload['scripts']] == ['BUY', 'SELL']
    assert all(s['quantity'] == 25 and s['productType'] == 'MARGIN' for s in payload['scripts'])
    client.place_order.assert_not_called()


@pytest.mark.parametrize('direction', ['BULLISH', 'BEARISH'])
def test_cheaper_fallback_checks_real_margin(client, chain, direction):
    chain = cheaper_chain(chain, direction)
    client.get_fund_limits.return_value = {'data': {'availabelBalance': 7000}}
    client.dhan_http.post.side_effect = [{'total_margin': 10000}, {'total_margin': 5000}]
    sell, buy, funding = strategy.affordable_option_legs(client, chain, direction, 25)
    assert sell['last_price'] == 50
    assert buy['security_id'] == 'cheap-hedge'
    assert funding['required_funds'] == 5932.50
    assert funding['affordable']
    client.place_order.assert_not_called()


@pytest.mark.parametrize('balance', [0, 5000])
def test_no_affordable_spread_never_places_orders(config, client, chain, analysis, monkeypatch, balance):
    cheaper_chain(chain, 'BULLISH')
    client.get_fund_limits.return_value = {'data': {'availabelBalance': balance}}
    monkeypatch.setattr(strategy, 'dhan_request', Mock(side_effect=[{'data': ['2026-10-27', '2026-11-03']}, chain]))
    assert strategy.entry_check(replace(config, dry_run=False, live_trading_enabled=True), client, analysis=analysis) == 1
    client.place_order.assert_not_called()


@pytest.mark.parametrize('response', [None, {}, {'status': 'failure', 'data': {'total_margin': 1}},
                                     {'total_margin': 'nan'}, {'total_margin': -1}, {'total_margin': 0},
                                     {'total_margin': True}, {'total_margin': 'inf'}])
def test_bad_margin_fails_closed(client, chain, response):
    client.dhan_http.post.return_value = response
    with pytest.raises(ValueError):
        strategy.place_credit_spread_entry(client, *strategy.option_legs(chain, 'BULLISH'), 25)
    client.place_order.assert_not_called()


@pytest.mark.parametrize('balance', [None, 'nan', 'inf', -1, True, 'bad'])
def test_bad_funds_fail_closed(client, chain, balance):
    client.get_fund_limits.return_value = {'data': {'availabelBalance': balance}}
    with pytest.raises(ValueError):
        strategy.place_credit_spread_entry(client, *strategy.option_legs(chain, 'BULLISH'), 25)
    client.place_order.assert_not_called()


def test_balance_drops_before_submission(config, client, chain, analysis, monkeypatch):
    client.get_fund_limits.side_effect = [{'availabelBalance': 100000}, {'availabelBalance': 0}]
    monkeypatch.setattr(strategy, 'dhan_request', Mock(side_effect=[{'data': ['2026-10-27', '2026-11-03']}, chain]))
    assert strategy.entry_check(replace(config, dry_run=False, live_trading_enabled=True), client, analysis=analysis) == 1
    client.place_order.assert_not_called()


def test_fallback_enters_hedge_first(config, client, chain, analysis, monkeypatch):
    cheaper_chain(chain, 'BULLISH')
    client.get_fund_limits.return_value = {'availabelBalance': 7000}
    client.dhan_http.post.side_effect = [{'total_margin': 10000}, {'total_margin': 5000}, {'total_margin': 5000}]
    monkeypatch.setattr(strategy, 'dhan_request', Mock(side_effect=[{'data': ['2026-10-27', '2026-11-03']}, chain]))
    assert strategy.entry_check(replace(config, dry_run=False, live_trading_enabled=True), client, analysis=analysis, reentry=True) == 0
    assert [(c.kwargs['security_id'], c.kwargs['transaction_type']) for c in client.place_order.call_args_list] == [('cheap-hedge', 'BUY'), ('102', 'SELL')]


def test_low_premium_alone_is_not_affordability(client, chain):
    cheaper_chain(chain, 'BULLISH')
    client.get_fund_limits.return_value = {'availabelBalance': 7000}
    with pytest.raises(ValueError, match='No affordable'):
        strategy.affordable_option_legs(client, chain, 'BULLISH', 25)
    assert client.dhan_http.post.call_count == 2
    client.place_order.assert_not_called()


def test_pending_order_blocks_funding_and_submission(client, chain):
    client.get_order_list.return_value = {'data': [{'exchangeSegment': 'NSE_FNO', 'orderStatus': 'PENDING'}]}
    with pytest.raises(ValueError):
        strategy.place_credit_spread_entry(client, *strategy.option_legs(chain, 'BULLISH'), 25)
    client.dhan_http.post.assert_not_called()
    client.place_order.assert_not_called()
