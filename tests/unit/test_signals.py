from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from dhan_algo.market import cross_signal, current_direction
from dhan_algo.options import next_expiry, option_legs, spread_payoff
from dhan_algo.positions import (short_legs_at_half_price, opposite_supertrend_cross,
    bought_option_expiring_within_two_days, dhan_positions, active_strategy_position)


@pytest.mark.parametrize("prior,latest,expected", [(99,101,"BULLISH"),(100,101,"BULLISH"),
    (101,99,"BEARISH"),(100,99,"BEARISH"),(99,100,None),(101,100,None),(101,102,None),(99,98,None)])
def test_cross_boundaries(prior, latest, expected):
    assert cross_signal([{"close": prior},{"close": latest}], [100,100]) == expected


@pytest.mark.parametrize("values", [[None,100],[100,None]])
def test_missing_indicator_cannot_cross(values):
    assert cross_signal([{"close":99},{"close":101}], values) is None


@pytest.mark.parametrize("close,expected", [(101,"BULLISH"),(100,"BEARISH"),(99,"BEARISH")])
def test_direction(close, expected):
    assert current_direction([{"close": close}], [100]) == expected


def test_missing_direction():
    with pytest.raises(ValueError):
        current_direction([{"close":1}], [None])


@pytest.mark.parametrize("direction,sell_id,buy_id", [("BULLISH","101","102"),("BEARISH","201","203")])
def test_option_selection_and_payoff(chain, direction, sell_id, buy_id):
    sell, buy = option_legs(chain,direction)
    assert (sell["security_id"],buy["security_id"]) == (sell_id,buy_id)
    assert spread_payoff(sell,buy,25) == dict(net_credit_per_unit=48,maximum_profit=1200,
        maximum_loss=1300,profit_booking_debit_per_unit=24,profit_booking_amount=600,spread_width=100)


@pytest.mark.parametrize("chain", [{}, {"data":{"oc":{"25000":{"pe":{"security_id":"1","greeks":{"delta":-.5},"last_price":100}}}}}])
def test_missing_hedge_rejected(chain):
    with pytest.raises(ValueError): option_legs(chain,"BULLISH")


@pytest.mark.parametrize("bid,ask", [(None,10),(20,None),(10,20),(20,20),(200,20)])
def test_invalid_payoff(bid,ask):
    with pytest.raises(ValueError):
        spread_payoff(dict(strike=100,top_bid_price=bid),dict(strike=0,top_ask_price=ask),25)


def test_next_expiry():
    assert next_expiry({"data":["2026-10-20","2026-10-06","2026-10-13"]}) == "2026-10-13"


@pytest.mark.parametrize("expiries", [[],["2026-10-06"]])
def test_missing_next_expiry(expiries):
    with pytest.raises(ValueError): next_expiry({"data":expiries})


@pytest.mark.parametrize("price,hit", [(52.5,True),(52.5001,False),(50,True),(20,True),(0,False),(-1,False),(None,False)])
def test_profit_target_boundary(client,positions,price,hit):
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"]["101"]["last_price"] = price
    assert short_legs_at_half_price(client,positions)[0] is hit


def test_all_shorts_must_hit(client,positions):
    positions.append({**positions[0],"securityId":"103"})
    client.ticker_data.return_value["data"]["data"]["NSE_FNO"] = {"101":{"last_price":40},"103":{"last_price":80}}
    assert not short_legs_at_half_price(client,positions)[0]
    assert short_legs_at_half_price(client,[])[0] is False


@pytest.mark.parametrize("option,cross,hit", [("PUT","BEARISH",True),("PUT","BULLISH",False),
    ("CALL","BULLISH",True),("CALL","BEARISH",False),("PUT",None,False)])
def test_opposite_cross(analysis,positions,option,cross,hit):
    positions[0]["drvOptionType"] = option
    assert opposite_supertrend_cross(replace(analysis,crossover_signal=cross),positions) is hit


@pytest.mark.parametrize("days,hit", [(-1,False),(0,True),(1,True),(2,True),(3,False)])
def test_expiry_boundaries(positions,days,hit):
    positions[1]["drvExpiryDate"] = (datetime.now(ZoneInfo("Asia/Kolkata")).date()+timedelta(days=days)).isoformat()
    assert bought_option_expiring_within_two_days(positions) is hit


def test_expiry_ignores_short_and_invalid(positions):
    positions[0]["drvExpiryDate"] = datetime.now().date().isoformat()
    positions[1]["drvExpiryDate"] = "invalid"
    assert not bought_option_expiring_within_two_days(positions)


@pytest.mark.parametrize("payload", [{"status":"failure","data":[]},{"data":{}},{"data":[None]}])
def test_failed_positions_fail_closed(client,payload):
    client.get_positions.return_value=payload
    with pytest.raises(ValueError): dhan_positions(client)


def test_active_positions(positions):
    assert active_strategy_position(positions)
    assert not active_strategy_position([{**p,"netQty":0} for p in positions])
    assert not active_strategy_position([{**p,"exchangeSegment":"NSE_EQ"} for p in positions])
