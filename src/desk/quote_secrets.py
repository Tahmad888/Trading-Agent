"""Credential-text guard shared by diagnostics and persistent market-data evidence."""
import os

CREDENTIAL_NAMES = ("TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN", "APCA_API_KEY_ID",
                    "APCA_API_SECRET_KEY", "WEBULL_APP_KEY", "WEBULL_APP_SECRET", "WEBULL_ACCESS_TOKEN",
                    "ALPHAVANTAGE_API_KEY", "MASSIVE_API_KEY", "TRADIER_ACCESS_TOKEN")
CREDENTIAL_MARKERS = ("bearer ", "authorization:", "access_token", "refresh_token", "client_secret", "token=",
                      "api_key=", "apikey=")

def credential_free(text: str, env=None) -> bool:
    env = os.environ if env is None else env
    lowered = text.lower()
    values = [env.get(name) for name in CREDENTIAL_NAMES]
    if any(value and len(value) >= 6 and value in text for value in values):
        return False
    return not any(marker in lowered for marker in CREDENTIAL_MARKERS)
