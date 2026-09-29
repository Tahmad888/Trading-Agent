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
