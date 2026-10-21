# Dhan NIFTY credit-spread strategy

Two executable scripts share configuration and order handling in `utils.py`.
The account is assumed to be dedicated to this strategy: pending F&O orders
block new submissions, and exits close its active NSE_FNO positions.

`strategy.py` handles entries, Supertrend stop-losses and expiry exits on the
existing main strategy schedule. Each invocation performs one cycle:

1. Read current Dhan positions.
2. Build completed 30-minute NIFTY candles from Dhan's 15-minute data.
3. Calculate Supertrend (ATR period 22, multiplier 4 by default).
4. Enter a next-expiry credit spread when no spread is active.
5. Exit an active spread when an opposite Supertrend condition occurs or the
   long option expires within two days.
6. Confirm the exits and two consecutive flat position reads, then re-enter.

`profit_booking.py` checks positions and current option prices every five
minutes. It books the spread when combined running profit reaches **50% of
initial credit profit**. This threshold is fixed for now. It uses the actual
matched position quantity, requires one matching short/long option pair, and
does not fetch candles, calculate Supertrend, enter or immediately re-enter.
After a successful profit exit, the next main strategy cycle may enter again.
Both calculations exclude brokerage, taxes and execution slippage; a target
observed from LTP does not guarantee that exact realised profit.

For entry, the protective BUY must be fully traded before submitting the short
SELL. For exit, the short BUY must be fully traded **and reflected in positions**
before submitting the hedge SELL. Dhan can reject selling the hedge while the
protected short is still open, even when the short-closing order was acknowledged.
The sequencing comments in both execution functions explain this requirement.

Both executables retain the existing live-order flags: `DRY_RUN=false` and
`LIVE_TRADING_ENABLED=true` are required to submit orders.

## Rejected orders and incomplete exits

Shared execution polls each order once per second for up to five reads and
checks position updates before proceeding to a dependent leg. Logs include the
broker's `omsErrorCode` and `omsErrorDescription`, order ID, filled quantity and
remaining quantity.

Known hedge and temporary rejections can trigger a fresh order after reconciling
the position and checking for outstanding orders, up to three total submissions
per leg in that invocation. Only the remaining quantity is submitted. Hedge
retries require the short to be closed. The executor completes a triggered exit
without rechecking the profit threshold between legs.

Pending orders, uncertain submission outcomes, permanent failures such as
insufficient funds, and unknown rejection reasons stop execution with a failed
job instead of blindly placing another order. Re-entry requires a confirmed exit.

There is no persistent exit-recovery state in this version. If an invocation
ends after closing only one leg, a later profit run reports the incomplete pair
as an error; inspect the broker positions and resolve it before expecting normal
profit checks to resume. Within a running invocation, retryable failures are
handled as described above.

## Runtime configuration

Both scripts use configuration from `utils.py`. Supertrend and index settings
are only used by the main strategy:

- Secrets: `CLIENT_ID`, `DHAN_ACCESS_TOKEN`
- Required variables: `NIFTY_QUANTITY`, `NIFTY_LOT_SIZE`, `DRY_RUN`,
  `LIVE_TRADING_ENABLED`
- Optional variables: `NIFTY_SECURITY_ID` (default `15`),
  `NIFTY_EXCHANGE_SEGMENT` (default `IDX_I`), `NIFTY_INSTRUMENT` (default
  `INDEX`), `NIFTY_OPTION_UNDERLYING_SECURITY_ID` (default `13`),
  `SUPERTREND_ATR_PERIOD` (default `22`), `SUPERTREND_MULTIPLIER` (default
  `4`), `SUPERTREND_LOOKBACK_DAYS` (default `60`), and
  `DHAN_REQUEST_TIMEOUT_SECONDS` (default `15`).

Run one cycle locally with:

```bash
python strategy.py
python profit_booking.py
```

## GitHub Actions

`.github/workflows/dhan-strategy.yml` checks out the latest commit and runs
`strategy.py` with the manually managed self-hosted-runner interpreter at
`/home/github/venvs/dhan-algo/bin/python`.
Its checked-in trigger remains manual (`workflow_dispatch`); retain the existing
external 30-minute dispatch schedule if that is how main cycles are launched.

`.github/workflows/dhan-profit-booking.yml` runs `profit_booking.py` every five
minutes from 09:15 through 15:30 IST, Monday through Friday, and supports manual
dispatch. The executable also skips runs outside that session. GitHub scheduled
runs can be delayed; this is a five-minute requested cadence, not a guarantee of
execution at the exact minute. The schedule takes effect once the workflow is on
the default branch and Actions scheduling is enabled.

Both workflows share the `dhan-strategy` concurrency group with
`cancel-in-progress: false`. A shared process lock in the runner's temporary
directory also prevents simultaneous manual execution for the same account.
The lock carries no exit-recovery state. Keep both workflows on the same
self-hosted machine.

`.github/workflows/refresh-dhan-token.yml` runs at 08:00 IST Monday through
Friday, calls `generate_access_token` from the same script, and stores the
result in the `main` environment's `DHAN_ACCESS_TOKEN` secret.

The checkout step fetches the workflow's selected commit automatically; no
manual `git pull` is needed in the runner's `_work` directory.

If dependencies change, update the server-managed environment manually:

```bash
cd /home/github/actions-runner/_work/dhan-algo/dhan-algo
/home/github/venvs/dhan-algo/bin/python -m pip install -r requirements-dev.txt
```

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/unit --cov=strategy --cov=utils --cov=profit_booking --cov-report=term-missing
```

Real read-only Dhan integration checks are opt-in:

```bash
python -m pytest tests/integration --dhan-integration -v
```
