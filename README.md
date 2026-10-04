# Dhan multi-strategy runner

Run one scheduled cycle through `main.py`. Strategies implement an ABC, have independent parameters, and run sequentially within the same invocation. One strategy failing produces a nonzero process exit code while the remaining strategies still run.

## Layout

```text
main.py                         CLI entry point
scripts/setup_project.py        One-time local environment setup
config/strategies.json          Enabled strategy instances
.env.example                    Credential/settings template
requirements.txt                Runtime dependencies
requirements-dev.txt            Test dependencies
pyproject.toml                  Pytest configuration

dhan_algo/
  config.py                     Validated settings and environment loading
  auth.py                       Runtime token generation
  broker.py                     Dhan SDK facade, scopes and dry-run guard
  http.py                       Dhan historical/option-chain requests
  market.py                     Candles and Supertrend
  options.py                    Contract selection and payoff calculation
  positions.py                  Position parsing and exit predicates
  execution.py                  Ordered leg execution and fill confirmation
  runner.py                     Strategy registry and portfolio orchestration
  audit.py                      Structured logs
  strategies/
    base.py                     Strategy ABC
    credit_spread.py            Existing NIFTY strategy

tests/
  unit/                         Deterministic, network-blocked scenarios
  integration/                  Opt-in real Dhan reads, orders blocked
```

## One-time setup

Use Python **3.12 or 3.13** (required by the pinned pandas-ta dependency):

```sh
python3.13 scripts/setup_project.py
```

This creates `.venv`, installs runtime/test dependencies and copies `.env.example` to `.env` only if it does not already exist. It does not contact Dhan. Alternatively, create a virtual environment yourself and install `requirements-dev.txt`.

Enable TOTP once in Dhan's account settings if using token generation. Set `CLIENT_ID` and either `DHAN_ACCESS_TOKEN`, or both `DHAN_PIN` and `DHAN_TOTP_SECRET`. Set `NIFTY_LOT_SIZE` to the current contract lot size. `NIFTY_QUANTITY` is the number of lots; order units are lots × lot size. Verify instrument IDs against the contracts you intend to use; the existing project's ID defaults are preserved.

Keep credentials in the ignored `.env` or your deployment secret store. Tokens expire, so authentication remains a runtime step rather than a one-time setup. A supplied token takes precedence over PIN/TOTP generation. The application does not automatically load `.env`; export it explicitly:

```sh
set -a
. ./.env
set +a
.venv/bin/python main.py
```

Both flags default to safe settings: `DRY_RUN=true`, `LIVE_TRADING_ENABLED=false`. Production order submission requires **both** `DRY_RUN=false` and `LIVE_TRADING_ENABLED=true`.

## Configure multiple strategies

`config/strategies.json` contains a list of named instances. The default runs the original credit spread on a **dedicated account**: without `security_ids`, it manages all NSE F&O positions, preserving the original behavior.

Multiple strategies require explicit, nonempty, disjoint `security_ids`. The broker filters positions, restricts orders, and the credit spread filters option-chain candidates to these IDs. Dhan nets account positions by instrument; shared contracts cannot be attributed safely to separate strategies in this implementation. Manual trades in an assigned contract are also included in that strategy's scope. Refresh the configured contract IDs as expiries roll; include any still-open legs. Out-of-scope positions are never closed by a scoped strategy.

Example structure (IDs below are illustrative and must be replaced with actual contracts):

```json
[
  {
    "name": "nifty_fast",
    "type": "credit_spread",
    "security_ids": ["101", "102", "201", "203"],
    "parameters": {"supertrend_atr_period": 10, "nifty_quantity": 1}
  },
  {
    "name": "nifty_slow",
    "type": "credit_spread",
    "security_ids": ["301", "302", "401", "403"],
    "parameters": {"supertrend_atr_period": 22, "nifty_quantity": 1}
  }
]
```

Run any portfolio file with:

```sh
.venv/bin/python main.py --strategies config/strategies.json
```

To add a different algorithm, subclass `Strategy` in `dhan_algo/strategies/`, implement `run_cycle(self, broker) -> int`, and add its class to `STRATEGIES` in `dhan_algo/runner.py`. Use the supplied broker for positions and order operations so scope and dry-run checks apply. The same ABC supports multiple instances of the same algorithm with different parameters. Runs are sequential, not concurrent threads, to avoid API bursts and get fresh position data for each instance.

## Preserved strategy rules and execution fixes

- Use completed, NSE-aligned 30-minute candles resampled from Dhan's 15-minute candles. Defaults: Supertrend ATR 22, multiplier 4, lookback 60 days.
- Entry uses the current Supertrend direction, **without requiring a fresh crossover**: bullish → put credit spread; bearish → call credit spread. Equality to Supertrend remains bearish, matching the original rule.
- Select the second listed expiry, short near absolute delta 0.50, and buy protection near half the short premium. Protection must now be on the correct out-of-the-money side. Reject missing contracts and invalid bid/ask credit payoffs.
- Exit when all eligible shorts trade at or below 52.5% of sell average, an opposite Supertrend crossover occurs, or a long option expires in 0–2 calendar days. Dates use Asia/Kolkata. Missing/zero quotes do not count as profit targets.
- Buy and confirm protection before selling the short. On exit, close and confirm the short before releasing protection. Submission failures, partial fills, rejections and timeouts stop subsequent legs.
- Re-entry requires fully traded exit orders and a fresh scoped position read showing no active positions. Pending orders are not automatically cancelled; after execution errors, reconcile orders/positions before retrying. There is no persistent order reconciliation service in this project.
- Dry runs report entry/exit intent without submitting or simulating fills. They do not create persistent paper positions or claim that live execution succeeded.

## Tests

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest --cov=dhan_algo --cov-report=term-missing
```

The default suite uses deterministic Dhan-shaped fixtures and prohibits network access. It covers both entry directions, expiry and contract selection, credit payoff validation, quantity conversion, crossover/equality boundaries, all eight exit-reason combinations, half-price tolerance, expiry day boundaries, malformed responses, every trading-flag combination, fill ordering, partial/rejected/cancelled/expired/pending orders, timeouts, re-entry gates, and multi-strategy isolation/failure handling. Order-lifecycle tests exercise execution against fakes, never the real broker.

To exercise **actual Dhan APIs in application dry-run mode**, export credentials and lot size, then run:

```sh
.venv/bin/python -m pytest tests/integration --dhan-integration -v
```

The integration suite forces both safe flags, blocks SDK order submission, and reads actual positions, candles, expiries and option chains. It evaluates entry and, when positions exist, exit logic against current data. Missing credentials or API/data entitlement errors fail an explicitly requested integration run. Ordinary test runs skip it. Actual market data cannot deterministically produce every entry/exit scenario, so boundary and failure coverage comes from the fixture suite.

Dhan's [production order API](https://dhanhq.co/docs/v2/orders/) does not document a `dry_run` order parameter. This project uses real read APIs and suppresses production writes locally. Dhan also provides a [sandbox](https://dhan.co/support/platforms/dhanhq-api/what-can-i-test-using-the-dhanhq-sandbox/); this repository does not pretend its read-only integration tests are sandbox order fills.

## Scheduling

The existing `.github/workflows/dhan-strategy.yml` invokes `main.py` on a self-hosted runner, keeping its original 13 weekday cycles from 09:16 to 15:16 IST. Set the environment secrets/variables shown in that workflow. Configure the committed strategy JSON for multiple instances. Workflow concurrency prevents overlapping runs of that workflow; coordinate any other schedulers separately. `.github/workflows/tests.yml` runs the offline suite on pushes and pull requests with no Dhan credentials.
