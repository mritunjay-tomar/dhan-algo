# Run the strategy with GitHub Actions

`main.py` reads ordinary environment variables and imports the adjacent
`strategy.py`. The original root-level Dhan Cloud files remain unchanged.
The strategy rules are copied from that version; date calculations explicitly
use Asia/Kolkata so they also work on runners whose system clock uses UTC.

## GitHub setup

1. Push `gha/`, `.github/workflows/dhan-strategy.yml`, and the root
   `requirements.txt` to the repository's default branch.
2. Open **Settings → Secrets and variables → Actions → Secrets** and add
   `CLIENT_ID`, `DHAN_PIN`, and `DHAN_TOTP_SECRET`. The PIN is the six-digit
   Dhan PIN. The TOTP secret is the key shown when setting up TOTP, not a
   six-digit code from an authenticator app. Never store these as repository
   variables or commit them to a file.
3. Under **Variables**, optionally set the settings below. Copy the intended
   values from your Dhan Cloud project; its configuration is not transferred
   automatically. Both trading flags default to dry-run operation.
4. Open **Actions → Dhan strategy → Run workflow** for a manual run.
   Choose whether that run is a dry run and whether live trading is enabled.
   The safe defaults are `dry_run=true` and `live_trading_enabled=false`.
   Configuration errors or a failed strategy cycle produce a failed job.

## Automatic token generation

Before every scheduled or manual strategy cycle, `gha/main.py` creates a
current six-digit TOTP and calls Dhan's documented
`POST https://auth.dhan.co/app/generateAccessToken` endpoint. Dhan returns a
fresh access token valid for 24 hours. The token exists only inside that job;
it is not printed, committed, uploaded, or written back to GitHub Secrets.
This avoids the manual renewal cycle and does not depend on a previous token
remaining valid.

TOTP must first be enabled once in **Dhan Web → DhanHQ Trading APIs → Setup
TOTP**. See [Dhan's authentication documentation](https://dhanhq.co/docs/v2/authentication/).

| Repository variable | Default |
| --- | --- |
| `NIFTY_SECURITY_ID` | `15` (preserved from Cloud main.py) |
| `NIFTY_EXCHANGE_SEGMENT` | `IDX_I` |
| `NIFTY_INSTRUMENT` | `INDEX` |
| `NIFTY_OPTION_UNDERLYING_SECURITY_ID` | `13` |
| `NIFTY_QUANTITY` | `1` (units, as in the original strategy) |
| `SUPERTREND_ATR_PERIOD` | `22` |
| `SUPERTREND_MULTIPLIER` | `4` |
| `SUPERTREND_LOOKBACK_DAYS` | `60` |
| `DHAN_REQUEST_TIMEOUT_SECONDS` | `15` |
| `DRY_RUN` | `true` |
| `LIVE_TRADING_ENABLED` | `false` |

The copied strategy submits orders only when `DRY_RUN=false` and
`LIVE_TRADING_ENABLED=true`. It reads current positions from Dhan each run;
it does not require a persisted local state file. Logs appear in the Actions
job output. Concurrency prevents this workflow from overlapping with itself,
but does not coordinate with the existing Dhan Cloud scheduler. Disable the
Cloud schedule before switching live execution to GitHub Actions.

Dhan documents that a whitelisted static IP is mandatory for order placement.
GitHub-hosted runners do not provide a stable, unique outbound IP, so use a
self-hosted runner with your whitelisted static IP for live trading. The
default dry-run mode can run on `ubuntu-latest` because it does not submit
orders.

## Schedule

Monday–Friday in **Asia/Kolkata (IST)**:

- AM: 09:16, 09:46, 10:16, 10:46, 11:16, 11:46.
- PM: 12:16, 12:46, 13:16, 13:46, 14:16, 14:46, 15:16.

There are 13 scheduled cycles per weekday. The schedule does not exclude
exchange holidays. GitHub supports the explicit `timezone` field in the
[workflow schedule syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).
Scheduled workflows run from the default branch and can be delayed or dropped
under load; GitHub Actions does not guarantee execution at the exact minute.
Public repositories' schedules are disabled after 60 days without repository
activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

## Run locally

Use Python 3.10 or newer (the workflow installs Python 3.12):

```sh
python3 -m pip install -r requirements.txt
# Supply CLIENT_ID, DHAN_PIN, and DHAN_TOTP_SECRET through your shell environment.
python3 gha/main.py
```

The entry point does not load the root `.env` file. Set environment variables
explicitly; the old root `.env.example` describes a different authentication
setup and is not the configuration reference for these scripts.
