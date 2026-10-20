import sys
from types import SimpleNamespace
from unittest.mock import Mock

import strategy


def test_main_runs_single_strategy(config, monkeypatch):
    client = Mock()
    monkeypatch.setitem(
        sys.modules,
        "dhanhq",
        SimpleNamespace(DhanContext=lambda *_: object(), dhanhq=lambda *_: client),
    )
    monkeypatch.setattr(strategy, "load_config", lambda: config)
    monkeypatch.setattr(strategy, "dhan_positions", Mock(return_value=[]))
    run = Mock(return_value=0)
    monkeypatch.setattr(strategy, "run_strategy", run)

    assert strategy.main() == 0
    run.assert_called_once_with(config, client, positions=[])


def test_main_reports_failure(monkeypatch):
    monkeypatch.setattr(strategy, "load_config", Mock(side_effect=ValueError("missing settings")))
    assert strategy.main() == 1
