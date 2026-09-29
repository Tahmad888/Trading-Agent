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

The Friday leader scan (16:40 ET) writes the watchlist to `data/watchlist.json`; until its first run the
scanner watches SPY, QQQ and IWM. To add or drop names, edit `data/taz-picks.json`:
`{"add": ["TSLA"], "remove": ["XYZ"]}` (SPY, QQQ and IWM always stay). Each morning at 10:00 the day's
movers are checked for an episodic pivot on top of the list.

## Plan B

If the iMac misses scans, the free Microsoft VM runs the same command from cron
(`*/5 * * * 1-5 ~/Trading-Agent/ops/imac/run-scan.sh`), and the missed slots show in the Friday funnel.
