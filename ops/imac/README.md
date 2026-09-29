# Running the scanner on the iMac

The iMac runs the desk's scanner (Taz, 29 Sep 2026). launchd starts `run-scan.sh` every 5 minutes; the
scanner itself decides whether a scan is due (every 15 minutes from 09:45 to 15:45 ET, and 16:10 ET after the
close, on trading days) and skips a slot that already ran. Every scan, failed ones included, goes to
`data/scan-log.jsonl`, which the Friday note reads.

## One-time setup

1. Keep the Mac awake: System Settings > Energy > "Prevent automatic sleeping when the display is off".
2. Install Python 3.11 or newer (python.org installer or Homebrew).
3. Get the code and install it:
   ```
   git clone https://github.com/Tahmad888/Trading-Agent.git ~/Trading-Agent
   cd ~/Trading-Agent && git checkout claude/project-thread-837gow
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
   ```
4. Put the Webull keys in a file only you can read (never in the repo):
   ```
   mkdir -p ~/.config/trading-desk && touch ~/.config/trading-desk/env && chmod 600 ~/.config/trading-desk/env
   open -e ~/.config/trading-desk/env
   ```
   with these lines, filled in:
   ```
   export WEBULL_APP_KEY=
   export WEBULL_APP_SECRET=
   export WEBULL_HOST=api.sandbox.webull.com
   ```
   Drop the `WEBULL_HOST` line when the real account's key replaces the paper one.
5. Start it: copy `com.tradingdesk.scanner.plist` to `~/Library/LaunchAgents/`, replace `USERNAME` with
   your Mac user name, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tradingdesk.scanner.plist`.
6. Check it: `tail ~/Trading-Agent/data/scanner.out` shows "no scan due" outside market hours and one line per scan in session.

The watchlist is `data/watchlist.json`, a JSON list of tickers; until the Friday leader scan writes it,
the scanner watches SPY, QQQ and IWM.

## Plan B

If the iMac misses scans, the free Microsoft VM runs the same command from cron
(`*/5 * * * 1-5 ~/Trading-Agent/ops/imac/run-scan.sh`), and the missed slots show in the Friday funnel.
