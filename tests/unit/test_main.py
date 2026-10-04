from unittest.mock import Mock
import main


def test_main_runs_configured_portfolio(config,monkeypatch,tmp_path):
    path=tmp_path/"portfolio.json"; path.write_text("[]")
    monkeypatch.setattr(main,"load_config",lambda:config)
    load=Mock(return_value=["strategy"]);run=Mock(return_value=0)
    monkeypatch.setattr(main,"load_strategies",load)
    monkeypatch.setattr(main,"run_strategies",run)
    assert main.main(["--strategies",str(path)])==0
    load.assert_called_once_with(path,config)
    assert run.call_args.args[1]==["strategy"]


def test_main_reports_failure(monkeypatch):
    monkeypatch.setattr(main,"load_config",Mock(side_effect=ValueError("missing settings")))
    assert main.main([])==1
