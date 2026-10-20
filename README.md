# Dhan NIFTY credit-spread strategy

The complete trading algorithm and executable entry point live in
`strategy.py`. Each invocation performs one cycle:

1. Read current Dhan positions.
2. Build completed 30-minute NIFTY candles from Dhan's 15-minute data.
3. Calculate Supertrend (ATR period 22, multiplier 4 by default).
4. Enter a next-expiry credit spread when no spread is active.
5. Exit an active spread when its combined running profit reaches 50% of its
   maximum credit profit, an opposite Supertrend condition occurs, or the long
   option expires within two days.
6. Poll submitted exit orders once per second for five attempts, confirm the
   strategy is flat on two consecutive position reads, and then re-enter.

The protective option is bought before the short option is sold. Exit orders
close the short leg before selling the hedge. Live orders require both
`DRY_RUN=false` and `LIVE_TRADING_ENABLED=true`.

## Runtime configuration

`strategy.py` reads these environment variables:

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
```

## GitHub Actions

`.github/workflows/dhan-strategy.yml` checks out the latest commit and runs
`strategy.py` with the manually managed self-hosted-runner interpreter at
`/home/github/venvs/dhan-algo/bin/python`.

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
python -m pytest tests/unit --cov=strategy --cov-report=term-missing
```

Real read-only Dhan integration checks are opt-in:

```bash
python -m pytest tests/integration --dhan-integration -v
```
