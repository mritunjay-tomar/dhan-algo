import json
from unittest.mock import Mock, MagicMock
from urllib.error import HTTPError, URLError
import pytest
from dhan_algo import auth, http, market


def response(payload):
    result=MagicMock()
    result.__enter__.return_value.read.return_value=json.dumps(payload).encode()
    return result


def test_auth_request_and_token(monkeypatch,caplog):
    opener=Mock(return_value=response({"accessToken":"secret-token","expiryTime":"tomorrow"}))
    monkeypatch.setattr(auth,"urlopen",opener)
    with caplog.at_level("INFO"):
        assert auth.generate_access_token("client","123456","JBSWY3DPEHPK3PXP",15)=="secret-token"
    request=opener.call_args.args[0]
    assert request.method=="POST"
    assert "dhanClientId=client" in request.full_url
    assert "secret-token" not in caplog.text
    assert "123456" not in caplog.text


@pytest.mark.parametrize("pin", ["123","abcdef",""])
def test_bad_pin(pin):
    with pytest.raises(ValueError):auth.generate_access_token("client",pin,"JBSWY3DPEHPK3PXP",15)


@pytest.mark.parametrize("payload", [{},[],{"accessToken":""}])
def test_missing_auth_token(monkeypatch,payload):
    monkeypatch.setattr(auth,"urlopen",Mock(return_value=response(payload)))
    with pytest.raises(RuntimeError):auth.generate_access_token("client","123456","JBSWY3DPEHPK3PXP",15)


@pytest.mark.parametrize("error", [HTTPError("https://example.invalid?pin=123456",401,"Unauthorized",{},None),URLError("offline")])
def test_auth_network_failure_does_not_expose_pin(monkeypatch,error):
    monkeypatch.setattr(auth,"urlopen",Mock(side_effect=error))
    with pytest.raises(RuntimeError) as raised:auth.generate_access_token("client","123456","JBSWY3DPEHPK3PXP",15)
    assert "123456" not in str(raised.value)


def test_invalid_auth_json(monkeypatch):
    bad=response({});bad.__enter__.return_value.read.return_value=b"not json"
    monkeypatch.setattr(auth,"urlopen",Mock(return_value=bad))
    with pytest.raises(RuntimeError,match="invalid JSON"):auth.generate_access_token("client","123456","JBSWY3DPEHPK3PXP",15)


def test_read_request_headers_body_and_no_token_log(monkeypatch,caplog):
    opener=Mock(return_value=response({"data":{"close":[1,2]}}))
    monkeypatch.setattr(http,"urlopen",opener)
    with caplog.at_level("INFO"):
        assert http.dhan_request("/charts/intraday","client","secret-token",{"interval":15},7)=={"data":{"close":[1,2]}}
    request=opener.call_args.args[0]
    assert json.loads(request.data)=={"interval":15}
    assert request.get_header("Access-token")=="secret-token"
    assert opener.call_args.kwargs["timeout"]==7
    assert "secret-token" not in caplog.text


def test_market_analysis_uses_completed_candles(config,monkeypatch):
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    start=datetime(2020,1,2,9,15,tzinfo=ZoneInfo("Asia/Kolkata"))
    rows={"open":[],"high":[],"low":[],"close":[],"timestamp":[]}
    for day in range(10):
        for interval in range(24):
            price=100+day*24+interval
            for key,value in {"open":price,"high":price+3,"low":price-2,"close":price+1,
                "timestamp":(start+timedelta(days=day,minutes=15*interval)).timestamp()}.items():
                rows[key].append(value)
    query=Mock(return_value=rows);monkeypatch.setattr(market,"dhan_request",query)
    analysis=market.analyse_market(config)
    assert len(analysis.candles)==120
    assert analysis.current_direction=="BULLISH"
    assert query.call_args.args[0]=="/charts/intraday"
    assert query.call_args.args[3]["interval"]==15
