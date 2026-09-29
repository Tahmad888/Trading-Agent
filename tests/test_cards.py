import re

from desk.playbook.cards import CARDS


def test_ten_cards_with_numbered_ids():
    assert len(CARDS) == 10
    assert sorted(int(i.split("_")[0]) for i in CARDS) == list(range(1, 11))
    assert all(re.fullmatch(r"\d+_[a-z0-9_]+", i) for i in CARDS)


def test_every_card_is_readable_and_labelled():
    for card in CARDS.values():
        assert card.rules and card.entry and card.stop and card.exit
        assert set(card.timeframes) == {"weekly", "daily", "1-hour / 15-minute"}
        for name, p in card.params.items():
            assert p.why, f"{card.id}.{name} says nothing about where it came from"
            if p.label == "Sourced":
                assert any(w in p.why for w in ("Kullamägi", "Minervini", "O'Neil", "Darvas", "Kell", "Luk",
                                                 "Street Smarts", "Weinstein", "Connors")), f"{card.id}.{name}"


def test_long_setups_that_need_the_trend_template():
    needs = {i for i, c in CARDS.items() if c.trend_template}
    assert needs == {"1_qullamaggie_breakout", "2_minervini_vcp", "3_oneil_cup_with_handle",
                     "4_darvas_box", "6_kell_ema_crossback", "7_luk_pullback_reclaim"}


# Frozen 29 Sep 2026 after the book check (playbook-candidates/3-book-check.md), before any paper ticket.
# A failing fingerprint means a card's rules or numbers changed: that needs Taz's approval (rule 5).
FROZEN = {
    "1_qullamaggie_breakout": "de428c0158df2942",
    "2_minervini_vcp": "efd353aecc2a6801",
    "3_oneil_cup_with_handle": "cd2f22a0883053d4",
    "4_darvas_box": "0478599fb5ff7b3f",
    "5_qullamaggie_episodic_pivot": "2cde513140dea18f",
    "6_kell_ema_crossback": "15e9a82c0ee3a6e2",
    "7_luk_pullback_reclaim": "80f3030c30269188",
    "8_raschke_holy_grail": "40eb8caa4f780f68",
    "9_weinstein_stage4_breakdown": "25d773e8a75b943c",
    "10_connors_rsi2": "6418884f8d4d3d22",
}


def test_cards_are_frozen():
    changed = [i for i, c in CARDS.items() if FROZEN.get(i) != c.fingerprint()]
    assert not changed, f"card rules changed without a new freeze: {changed}"


def test_filter_numbers_are_frozen():
    from desk.playbook import filters as f
    assert (f.ABOVE_52W_LOW, f.FROM_52W_HIGH, f.RISING_200_BARS, f.RS_AVG_BARS, f.RS_NEAR_HIGH,
            f.RISING_50_BARS) == (1.30, 0.75, 21, 50, 0.90, 5)
