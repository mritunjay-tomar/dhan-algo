# Dhan multi-strategy runner

Run one scheduled cycle through `main.py`. Strategies implement an ABC, have independent parameters, and run sequentially within the same invocation. One strategy failing produces a nonzero process exit code while the remaining strategies still run.

## What this strategy trades

The included strategy is a NIFTY **defined-risk credit spread** strategy. A credit spread earns a credit when it is opened, has a capped maximum loss, and is made from two options of the same type and expiry:

- A **bullish** NIFTY view opens a put credit spread: sell a higher-strike put and buy a lower-strike put as protection.
- A **bearish** NIFTY view opens a call credit spread: sell a lower-strike call and buy a higher-strike call as protection.

It is intended for NIFTY F&O only, runs on completed 30-minute NIFTY candles, and uses the current Supertrend direction. It is not a backtester, a paper-trading ledger, or a guarantee of a trade. Dhan may reject orders, prices can move between quote and fill, and the actual credit, margin and P&L can differ from the proposal. Use dry-run until you have independently reviewed the proposed contracts and quantities.

## Exact trading conditions

Every cycle first reads the strategy's current NIFTY F&O positions.

| Account state | Condition | Action |
| --- | --- | --- |
| No active position | Latest completed 30-minute NIFTY close is above Supertrend | Propose a next-expiry put credit spread. |
| No active position | Latest completed 30-minute NIFTY close is at or below Supertrend | Propose a next-expiry call credit spread. |
| Active spread | Every short option's LTP is at or below 52.5% of its recorded average sell price | Exit the spread to take profit. |
| Active put spread | Latest completed 30-minute NIFTY close is below Supertrend | Exit the spread. |
| Active call spread | Latest completed 30-minute NIFTY close is above Supertrend | Exit the spread. |
| Active spread | Any long option expires today, tomorrow or in two calendar days | Exit the spread. |
| Active spread | None of the exit conditions applies | Keep the position open. |

The entry direction does **not** require a new crossover. A close exactly equal to Supertrend is treated as bearish. Supertrend defaults to ATR period 22 and multiplier 4, using the prior 60 calendar days of 15-minute Dhan candles resampled into NSE-aligned, completed 30-minute candles.

After a live exit is fully confirmed and the scoped account is flat, the strategy immediately evaluates a re-entry using that same completed-candle direction. It can therefore open a new spread in the same scheduled cycle. It never re-enters after an incomplete exit or while an active scoped position remains.

For an entry, the strategy sorts the active expiries returned by Dhan and selects the second one. It chooses the short option whose delta is closest to -0.50 for puts or +0.50 for calls. It chooses an out-of-the-money protective option whose premium is closest to half the short option premium. It will refuse to trade if Dhan has not provided valid contracts or executable bid/ask prices, or if the result is not a positive credit spread.

Order quantity is `NIFTY_QUANTITY × NIFTY_LOT_SIZE`. Confirm the current lot size in Dhan before every expiry change; the application does not look it up automatically.

## Worked example: bullish put credit spread

This is an illustrative example only; strikes, premiums, contract IDs and lot sizes are deliberately made up.

1. The completed 10:15 IST NIFTY candle closes at 25,120 while Supertrend is 25,070. The direction is bullish.
2. The strategy selects the second expiry returned by Dhan, say 16 October.
3. It finds a 25,000 put near delta -0.50, with bid ₹100, and a lower 24,900 put with ask ₹52. It proposes selling the 25,000 put and buying the 24,900 put.
4. With `NIFTY_QUANTITY=1` and `NIFTY_LOT_SIZE=25`, it trades 25 units of each leg. The estimated credit is `(₹100 − ₹52) × 25 = ₹1,200`. Spread width is `₹100 × 25 = ₹2,500`, so the estimated maximum loss is `(₹100 − ₹48) × 25 = ₹1,300` before brokerage, taxes and slippage.
5. If the short put's recorded sell average is ₹100, the profit exit is triggered at or below `₹52.50` (half price plus 5% tolerance), provided every short leg reaches its own target. A completed candle closing below Supertrend or approaching long-leg expiry also triggers the exit.

In live mode the application submits the 24,900 protective put buy first and, once Dhan accepts that submission, immediately submits the 25,000 short-put sell. It does not wait for Dhan's order-status endpoint to report a fill between the two entry submissions. On exit it buys back and confirms the short put before selling the protective put. If an entry submission is rejected, or an exit order is partially filled, cancelled, expired, or not confirmed before the timeout, it stops; inspect Dhan orders and positions before trying again.

## First run: beginner checklist

1. Open a Dhan account with DhanHQ access, enable TOTP if you will generate access tokens, and ensure Dhan market-data/API access for NIFTY options is available.
2. Install Python 3.12 or 3.13 and run the one-time setup below.
3. Copy `.env.example` to `.env` if setup did not create it. Enter `CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET`, `NIFTY_LOT_SIZE`, and keep `DRY_RUN=true` plus `LIVE_TRADING_ENABLED=false`.
4. Check that `config/strategies.json` contains the default single strategy. Do not add `security_ids` for a dedicated strategy account.
5. Export the `.env` values and run `main.py`. Read the proposed expiry, legs, quantity, credit, maximum loss and profit-booking amount in the output.
6. Run the command during market hours several times with dry-run enabled. Dry-run uses live Dhan reads but never submits an order. It does not save a simulated position, so it can propose the same entry again on each independent run.
7. Before any live run, verify the expiry, strikes, quantities, Dhan margin requirement, available funds, Dhan's current order/API requirements, and that the account does not contain unrelated NSE F&O positions. Change the flags only when you explicitly intend live order submission: `DRY_RUN=false` and `LIVE_TRADING_ENABLED=true`.

The last step should be read literally: live submission happens only when `DRY_RUN=false` **and** `LIVE_TRADING_ENABLED=true`. Do not use the default unscoped strategy on an account that contains unrelated NIFTY F&O positions, because it regards those positions as its own.

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
- Exit when all eligible shorts trade at or below 52.5% of sell average, the latest completed candle is on the adverse side of Supertrend, or a long option expires in 0–2 calendar days. Dates use Asia/Kolkata. Missing/zero quotes do not count as profit targets.
- Submit the protective buy before the short sell, without waiting for entry fill confirmation. On exit, close and confirm the short before releasing protection. A rejected entry submission, or an incomplete exit, stops the remaining legs.
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

The existing `.github/workflows/dhan-strategy.yml` invokes `main.py` on a self-hosted runner, keeping its original 13 weekday cycles from 09:16 to 15:16 IST. Set the environment secrets/variables shown in that workflow. Configure the committed strategy JSON for multiple instances. Workflow concurrency prevents overlapping runs of that workflow; coordinate any other schedulers separately. `.github/workflows/tests.yml` ("Strategy tests") runs the unit suite and coverage on pushes, pull requests and manual dispatches using an ephemeral Ubuntu runner with no Dhan credentials.

For real API testing, open **Actions → Strategy tests → Run workflow**, enable **Also run real Dhan API tests in dry-run mode**, and start the workflow. After the unit suite passes, the integration job uses the same self-hosted runner, `main` environment, secrets, strategy variables and Python 3.12 settings as the trading workflow. It shares the trading workflow's concurrency lock. `DRY_RUN=true` and `LIVE_TRADING_ENABLED=false` are hardcoded for tests regardless of production variables. No additional secrets are required. Coverage and test results appear in the job logs.
