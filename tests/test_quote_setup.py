import shlex

import pytest

from tools.setup_tastytrade_env import save


def test_hidden_setup_preserves_other_settings_and_quotes_values(tmp_path):
    path = tmp_path/"private"/"env"
    path.parent.mkdir()
    old = "export SSL_CERT_FILE=/tmp/ca.pem\nexport WEBULL_APP_KEY='fixture-webull'\nexport APCA_API_KEY_ID='fixture-paper'\nexport MASSIVE_API_KEY='fixture-massive'\n"
    path.write_text(old+"TASTYTRADE_CLIENT_SECRET=old\nexport TASTYTRADE_REFRESH_TOKEN=old\n")
    secret, refresh = "new' secret $(do-not-execute)", "new-refresh"
    save(path, secret, refresh)
    text = path.read_text()
    assert text.startswith(old) and text.count("TASTYTRADE_CLIENT_SECRET=") == 1
    assert text.count("TASTYTRADE_REFRESH_TOKEN=") == 1
    assert shlex.split(text.splitlines()[-2].split("=",1)[1]) == [secret]
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("value", ["", "secret\nextra", "secret\0extra"])
def test_invalid_setup_does_not_change_existing_file(tmp_path, value):
    path = tmp_path/"env"
    path.write_text("preserve-me")
    with pytest.raises(ValueError):
        save(path, value, "refresh")
    assert path.read_text() == "preserve-me"


def test_symlink_refused_without_changing_target(tmp_path):
    target = tmp_path/"target"
    target.write_text("preserve-me")
    path = tmp_path/"env"
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        save(path, "secret", "refresh")
    assert target.read_text() == "preserve-me"
