"""The 10 wave-1 setup cards (blueprint v2.4 section 5, build step 4).

Serves the analyze and plan steps. Each card holds the setup's rules in plain
words and every number its trigger code uses, each with a label (rule 3):
Sourced (with the book, site or paper), Checked (on our data or journal), or
Assumption (the journal will check it). The trigger code in
desk.playbook.triggers reads its numbers from here, so a card and its code
can't disagree.

A card is frozen before its first paper ticket: tests/test_cards.py holds a
fingerprint of each card, and changing a rule or number breaks that test until
the fingerprint is updated. Setup rules change only when Taz approves (rule 5).

Plan B: if a card's trigger misfires on charts Taz can see, the card goes back
to journal-only (tier 0) while the rule is fixed with his approval.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

Label = Literal["Sourced", "Checked", "Assumption"]
Direction = Literal["long", "short"]


class Param(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: float
    label: Label
    why: str                      # the source for Sourced, or what the number means


class Card(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str                       # the setup_id on proposals and in the journal
    name: str
    family: Literal["breakout", "pullback", "falling market"]
    directions: tuple[Direction, ...]
    source: str
    grade: Literal["moderate", "unproven"]
    rules: tuple[str, ...]        # the setup in plain words, for tickets and Taz
    timeframes: dict[str, str]    # what each timeframe must show (rule 4)
    entry: str
    stop: str
    exit: str
    params: dict[str, Param]
    trend_template: bool = True   # longs must pass Minervini's Trend Template
    needs_earnings_numbers: bool = False
    index_etfs_only: bool = False

    def p(self, name: str) -> float:
        return self.params[name].value

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:16]


def S(value: float, why: str) -> Param:
    return Param(value=value, label="Sourced", why=why)


def A(value: float, why: str) -> Param:
    return Param(value=value, label="Assumption", why=why)


QULL = "Kullamägi's own site, qullamaggie.com"
MINERVINI = "Minervini, Trade Like a Stock Market Wizard (2013)"
ONEIL = "O'Neil, How to Make Money in Stocks, 4th ed. (2009)"
DARVAS = "Darvas, How I Made $2,000,000 in the Stock Market (1960)"
KELL = "Kell's teaching (Cycle of Price Action)"
LUK = "Luk's posts and interviews"
STREET_SMARTS = "Connors and Raschke, Street Smarts (1995), Holy Grail"
WEINSTEIN = "Weinstein, Secrets for Profiting in Bull and Bear Markets (1988)"
CONNORS = "Connors and Alvarez, Short Term Trading Strategies That Work (2008)"

DAILY_TREND = "Uptrend: passes the Trend Template"
INTRADAY_15_60 = "Entry: the first 15-minute or 1-hour bar's high is taken out"

CARDS: dict[str, Card] = {c.id: c for c in [
    Card(
        id="1_qullamaggie_breakout", name="Qullamaggie breakout", family="breakout",
        directions=("long",), source=QULL, grade="moderate",
        rules=("A 30%+ move in the last 1 to 3 months",
               "Then 2 weeks to 2 months of tightening range, riding the 10 and 20-day averages",
               "Buy the break of the base high on the first 15 or 60-minute high"),
        timeframes={"weekly": "Rising, near highs", "daily": "30%+ run, then a tight base above the 20-day",
                    "1-hour / 15-minute": INTRADAY_15_60},
        entry="Buy-stop-limit at the base high, on the first 15-minute or 1-hour high of a day that trades through it",
        stop="The day's low, no wider than one day's average range",
        exit="Sell a third after 3 to 5 days, stop to breakeven, trail the rest on the 10-day (20-day for calmer names)",
        params={
            "prior_move": S(0.30, f"{QULL}: stocks up 30%+ in 1-3 months"),
            "prior_move_bars": A(63, "3 months of daily bars before the base"),
            "base_min_bars": A(10, "2 weeks"), "base_max_bars": A(40, "2 months"),
            "tightening": A(0.6, "last 5 days' range under 0.6 × the base's first 10 days' range"),
            "above_20d_share": A(0.8, "close above the 20-day on 80% of base days"),
            "min_adr_pct": A(3.5, "he trades volatile names; ADR% under 3.5 doesn't pay"),
            "max_stop_adr": S(1.0, f"{QULL}: stop no wider than the ADR"),
        }),
    Card(
        id="2_minervini_vcp", name="Minervini VCP pivot", family="breakout",
        directions=("long",), source=MINERVINI, grade="moderate",
        rules=("Inside a Trend Template stock",
               "Pullbacks that shrink each time (for example 25%, 12%, 6%) with volume drying up",
               "Buy through the last pivot on rising volume"),
        timeframes={"weekly": "Stage 2 uptrend", "daily": "2+ shrinking contractions, quiet volume, near the pivot",
                    "1-hour / 15-minute": "Entry: price trades through the pivot on rising volume"},
        entry="Buy-stop-limit just above the pivot (the last contraction's high); no fill more than 5% past it",
        stop="Under the last contraction's low; skipped if that is more than 8% away",
        exit="Sell a third at +2R or +20%, stop to breakeven, trail the rest on a close below the 21-day EMA",
        params={
            "min_contractions": A(2, "at least two pullbacks inside the base"),
            "shrink": A(0.65, "each pullback no deeper than 0.65 × the one before"),
            "last_max_depth": A(0.10, "the last pullback 10% or less"),
            "pivot_bars": A(2, "a swing high or low is the extreme of 2 bars either side"),
            "base_lookback": A(150, "the base starts at the highest high of the last 150 bars (30 weeks)"),
            "base_min_bars": A(15, "3 weeks, O'Neil's shortest base"),
            "dry_volume": A(0.7, "10-day average volume under 0.7 × the 50-day"),
            "max_stop_pct": S(0.08, f"{MINERVINI}: never more than 8% away"),
            "max_chase": S(0.05, f"{ONEIL}: never more than 5% past the pivot"),
        }),
    Card(
        id="3_oneil_cup_with_handle", name="O'Neil cup-with-handle", family="breakout",
        directions=("long",), source=ONEIL, grade="moderate", needs_earnings_numbers=True,
        rules=("After a 30%+ advance, a rounded cup 12 to 33% deep over 7 to 65 weeks",
               "A handle in the upper half that drifts down 8 to 12% on light volume",
               "Buy through the handle high on volume 40 to 50% above average, never more than 5% past it",
               "Earnings gate: latest quarter's EPS up 25% or more"),
        timeframes={"weekly": "Cup 7-65 weeks after a 30%+ advance", "daily": "Handle in the cup's upper half on light volume",
                    "1-hour / 15-minute": "Entry: handle high taken out, volume pace 1.4× average or more"},
        entry="Buy-stop-limit at the handle high; no fill more than 5% past it",
        stop="7 to 8% below entry, or just under the handle low if that's closer",
        exit="Take most profits at 20-25%; a 20% gain within 3 weeks goes to Taz as a hold-longer decision",
        params={
            "prior_advance": S(0.30, f"{ONEIL}: a prior uptrend of at least 30%"),
            "prior_advance_bars": A(126, "the 30% advance is measured over the 6 months before the cup"),
            "cup_min_depth": S(0.12, ONEIL), "cup_max_depth": S(0.33, ONEIL),
            "cup_min_bars": S(35, f"{ONEIL}: 7 weeks"), "cup_max_bars": S(325, f"{ONEIL}: 65 weeks"),
            "right_side": A(0.85, "the right side recovers to within 15% of the left-side high"),
            "handle_min_bars": A(5, "a week"), "handle_max_bars": A(25, "5 weeks"),
            "handle_max_depth": S(0.12, f"{ONEIL}: handles drift down 8-12%"),
            "breakout_volume": S(1.4, f"{ONEIL}: volume 40-50% above average"),
            "max_chase": S(0.05, f"{ONEIL}: never more than 5% past the pivot"),
            "stop_pct": S(0.08, f"{ONEIL}: sell at 7-8% below the purchase price"),
            "min_eps_growth": S(0.25, f"{ONEIL}: current quarterly EPS up 25%+"),
        }),
    Card(
        id="4_darvas_box", name="Darvas box", family="breakout",
        directions=("long",), source=DARVAS, grade="moderate",
        rules=("A stock at new highs builds a box",
               "Buy the break of the box top",
               "Stop just under the box bottom, raised with each new box"),
        timeframes={"weekly": "New highs", "daily": "A box: a top with 3 lower bars after it, and a floor",
                    "1-hour / 15-minute": "Entry: the box top taken out on heavy volume"},
        entry="Buy-stop-limit at the box top",
        stop="Just under the box bottom; raised to each new box's bottom",
        exit="Only by the trailing stop",
        params={
            "new_high_bars": A(10, "the 52-week high was made in the last 10 days"),
            "confirm_bars": A(3, "a top is a high with 3 later bars all below it"),
            "max_height_adr": A(2.0, "box height no more than 2 × ADR%, or it's too loose"),
            "breakout_volume": A(1.5, "breakout day's volume 1.5 × the 50-day average"),
        }),
    Card(
        id="5_qullamaggie_episodic_pivot", name="Qullamaggie episodic pivot", family="breakout",
        directions=("long",), source=f"{QULL}, crediting Pradeep Bonde", grade="moderate",
        trend_template=False, needs_earnings_numbers=True,
        rules=("A stock that went sideways for 3 to 6 months",
               "Gaps up 10% or more on news that surprises the market, usually earnings with big growth",
               "Heavy early volume; buy the break of the first 15-minute high, or the first 1-hour high by 11:00"),
        timeframes={"weekly": "Sideways for 3-6 months", "daily": "Today's 10%+ gap on news",
                    "1-hour / 15-minute": "Entry: first 15-minute high, or first 1-hour high by 11:00 ET"},
        entry="Buy-stop-limit at the first 15-minute high; if not taken by 11:00 ET, the first 1-hour high; none after that",
        stop="The day's low, no wider than 1 to 1.5 days' average range",
        exit="Trail on the 10 or 20-day average",
        params={
            "min_gap": S(0.10, f"{QULL}: gaps of 10% or more"),
            "sideways_bars": A(60, "the 3 months before the gap"),
            "sideways_range": A(0.35, "the 60-day high-low range is 35% or less"),
            "near_middle": A(0.15, "the close is within 15% of the 60-day midpoint"),
            "early_volume": A(0.5, "first 30 minutes' volume is half the 50-day average or more"),
            "max_stop_adr": S(1.5, f"{QULL}: no more than 1x, max 1.5x the average daily range"),
            "min_growth": A(0.25, "the report shows EPS or sales up 25%+ year on year"),
        }),
    Card(
        id="6_kell_ema_crossback", name="Kell EMA crossback", family="pullback",
        directions=("long",), source=KELL, grade="moderate",
        rules=("A fresh uptrend with the 10-day EMA above the 20-day and both rising",
               "The first pullback to them",
               "Buy the break of that day's high on 1-hour bars"),
        timeframes={"weekly": "Turning up", "daily": "10 EMA over 20 EMA, both rising; first dip to them",
                    "1-hour / 15-minute": "Entry: the pullback day's high taken out on 1-hour bars"},
        entry="Buy-stop-limit at the pullback day's high",
        stop="Under the pullback low",
        exit="Sell a third to a half when price stretches 3 × ADR% above the 10-day EMA; out on two closes below the 20-day EMA",
        params={
            "rising_bars": A(5, "rising = higher than 5 bars ago"),
            "fresh_bars": A(40, "the 10 EMA crossed above the 20 EMA in the last 40 bars"),
            "touch_atr": A(0.25, "the day's low within 0.25 × ATR of the 10 or 20 EMA"),
            "stretch_adr": A(3.0, "extended: close 3 × ADR% above the 10 EMA"),
        }),
    Card(
        id="7_luk_pullback_reclaim", name="Luk pullback and reclaim", family="pullback",
        directions=("long",), source=LUK, grade="moderate",
        rules=("A stock up 30%+ over 1, 3 or 6 months",
               "Pulls back to its 21-day EMA or an anchored VWAP",
               "Reclaims it on a 15-minute close"),
        timeframes={"weekly": "Strong uptrend", "daily": "9 EMA over 21 over 50; pullback to the 21 EMA or anchored VWAP",
                    "1-hour / 15-minute": "Entry: a 15-minute close back above the level"},
        entry="Limit at the reclaim, after a 15-minute close back above the level",
        stop="The intraday low, no wider than 1 × ADR% (Luk: often 1-4%)",
        exit="Hold the stop until +2R, then out on a daily close below the 9-day EMA",
        params={
            "min_run": S(0.30, f"{LUK}: stocks up 30%+"),
            "near_atr": A(0.5, "the low within 0.5 × ATR of the 21 EMA or anchored VWAP"),
            "anchor_bars": A(63, "the VWAP is anchored at the lowest low of the last 3 months"),
            "max_stop_adr": A(1.0, "stop no wider than 1 × ADR%"),
        }),
    Card(
        id="8_raschke_holy_grail", name="Raschke Holy Grail", family="pullback",
        directions=("long", "short"), source=STREET_SMARTS, grade="unproven", trend_template=False,
        rules=("ADX above 30 and rising: a strong trend",
               "Price pulls back to the 20-day EMA",
               "Buy above the high of the bar that touched it; first target the prior swing high",
               "In a downtrend (−DI above +DI), the mirror: a rally to the 20-day EMA, traded with puts"),
        timeframes={"weekly": "Trending", "daily": "ADX > 30 at the swing, pullback touches the 20 EMA",
                    "1-hour / 15-minute": "Entry: the touch bar's high (low, going down) taken out"},
        entry="Buy-stop-limit above the touch bar's high (below its low for the down side)",
        stop="The swing low (swing high for the down side)",
        exit="Half at the prior swing high (low), trail the rest on a close through the 20 EMA; 10 trading days at most",
        params={
            "min_adx": S(30, f"{STREET_SMARTS}: ADX above 30"),
            "adx_rising_bars": A(5, "rising = ADX at the swing high is above its value 5 bars earlier"),
            "swing_bars": A(10, "the swing high (low) is the extreme of the last 10 bars"),
            "touch_atr": A(0.25, "the bar comes within 0.25 × ATR of the 20 EMA"),
            "time_limit": A(10, "trading days"),
        }),
    Card(
        id="9_weinstein_stage4_breakdown", name="Weinstein stage 4 breakdown", family="falling market",
        directions=("short",), source=WEINSTEIN, grade="moderate", trend_template=False,
        rules=("A stock tops out: its 30-week average (about the 150-day) flattens",
               "Then breaks below the top's support on a daily close",
               "The 30-week average turning down, relative strength against SPY falling",
               "Only when the market filter is below full size"),
        timeframes={"weekly": "Stage 3 top turning to stage 4", "daily": "Close below the top's support",
                    "1-hour / 15-minute": "Entry: the next session, if price stays under support"},
        entry="Buy a put or put debit spread the next session, while price stays below support",
        stop="Above the last rally high",
        exit="A close back above the 30-week (150-day) average, or after 3 weeks",
        params={
            "top_bars": A(40, "the top's support is the lowest low of the 40 bars before today"),
            "turn_bars": A(5, "the 150-day is below its value 5 bars ago"),
            "rs_avg_bars": A(50, "RS line below its 50-day average = relative strength falling"),
            "rally_bars": A(10, "the last rally high is the highest high of the last 10 bars"),
            "time_limit": A(15, "3 weeks of trading days; Weinstein's holds ran months"),
        }),
    Card(
        id="10_connors_rsi2", name="Connors RSI(2) on index ETFs", family="falling market",
        directions=("long", "short"), source=CONNORS, grade="moderate",
        trend_template=False, index_etfs_only=True,
        rules=("SPY, QQQ, IWM or a sector ETF above its 200-day closes with 2-period RSI under 5: buy near the close",
               "Sell when it closes above its 5-day average",
               "Below the 200-day, the mirror: RSI(2) above 95, out on a close below the 5-day average",
               "The desk adds a stop at 2.5 × ATR(10) and a 10-day limit"),
        timeframes={"weekly": "Not used", "daily": "Above (below) the 200-day, RSI(2) under 5 (over 95)",
                    "1-hour / 15-minute": "Ticket at 15:45 ET on an estimate of the close"},
        entry="Limit near the close, sent at 15:45 ET",
        stop="2.5 × ATR(10) from entry",
        exit="A close above (below) the 5-day average, or 10 trading days",
        params={
            "rsi_low": S(5, f"{CONNORS}: RSI(2) under 5"), "rsi_high": S(95, f"{CONNORS}: the mirror, over 95"),
            "stop_atr": A(2.5, "the original has no stop; the desk adds one"),
            "time_limit": A(10, "trading days; the original has no limit"),
        }),
]}

INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP",
                        "XLU", "XLB", "XLRE", "XLC", "SMH"})
