"""Bounded SEC public JSON observations, not an earnings qualification adapter.

Sources: SEC EDGAR API documentation and Accessing EDGAR Data, recorded in 09b.
No keys, redirects, retries or automatic promotion of filing facts to trade evidence.
"""
from datetime import datetime, timezone
import json
import re
import time
from urllib import error, request

from desk.symbols import canonical_symbol

MAX_BYTES = 20 * 1024 * 1024
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


class SecError(ValueError):
    def __init__(self, code, http_status=None):
        self.code, self.http_status = code, http_status
        super().__init__(code)  # Never include a body, URL parameters or user contact.


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(req, timeout):
    with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise SecError("RESPONSE_TOO_LARGE")
    return data


def _reject_constant(value):
    raise ValueError("nonfinite JSON")


class SecData:
    def __init__(self, user_agent, *, transport=_fetch, monotonic=time.monotonic,
                 sleep=time.sleep, clock=lambda: datetime.now(timezone.utc)):
        # Operator supplies their own application/contact, not a fabricated identity.
        if not isinstance(user_agent, str) or not re.fullmatch(r"[\x20-\x7e]{5,250}", user_agent) or "@" not in user_agent:
            raise SecError("SEC_USER_AGENT_REQUIRED")
        self._user_agent, self._transport = user_agent, transport
        self._monotonic, self._sleep, self._clock = monotonic, sleep, clock
        self._last = None

    def _bytes(self, url, accept="application/json"):
        if self._last is not None:
            wait = .25 - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        req = request.Request(url, headers={"User-Agent": self._user_agent, "Accept": accept})
        try:
            raw = self._transport(req, 20)
        except error.HTTPError as exc:
            with exc:
                raise SecError("HTTP_ERROR", exc.code) from None
        except (error.URLError, OSError, TimeoutError):
            raise SecError("TRANSPORT_FAILURE") from None
        finally:
            self._last = self._monotonic()
        if len(raw) > MAX_BYTES:
            raise SecError("RESPONSE_TOO_LARGE")
        return raw

    def _get(self, url):
        raw = self._bytes(url)
        try:
            payload = json.loads(raw, parse_constant=_reject_constant)
            if not isinstance(payload, dict) or any(k in payload for k in ("error", "errors", "message", "Information")):
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            raise SecError("INVALID_JSON_RESPONSE") from None
        return {"source_url": url, "received_at": self._clock().isoformat(), "payload": payload}

    def document(self, cik, accession, filename):
        """Original public filing, confined to a validated SEC archive path."""
        if not re.fullmatch(r"\d{10}", str(cik)) or int(cik) == 0 or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            raise SecError("INVALID_DOCUMENT_IDENTITY")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:htm|html|txt)", filename):
            raise SecError("UNSUPPORTED_DOCUMENT_NAME")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"
        raw = self._bytes(url, "text/html,text/plain")
        try:
            body = raw.decode("utf-8")
            if not body.strip() or "Your Request Originates from an Undeclared Automated Tool" in body or "Request Rate Threshold Exceeded" in body:
                raise ValueError
        except (UnicodeError, ValueError):
            raise SecError("DOCUMENT_UNAVAILABLE") from None
        return {"source_url": url, "received_at": self._clock().isoformat(), "body": body}

    def ticker_index(self):
        return self._get(TICKERS_URL)

    @staticmethod
    def resolve(index, symbol):
        matches = [row for row in index["payload"].values()
                   if isinstance(row, dict) and isinstance(row.get("ticker"), str)
                   and canonical_symbol(row["ticker"]) == canonical_symbol(symbol)]
        if len(matches) != 1:
            raise SecError("IDENTITY_MISSING_OR_AMBIGUOUS")
        cik = matches[0].get("cik_str")
        if type(cik) is not int or not 0 < cik < 10**10:
            raise SecError("INVALID_CIK")
        return f"{cik:010d}"

    def observations(self, cik, kind, symbol):
        if not isinstance(cik, str) or not re.fullmatch(r"\d{10}", cik) or int(cik) == 0:
            raise SecError("INVALID_CIK")
        if kind not in {"submissions", "companyfacts"}:
            raise SecError("UNSUPPORTED_KIND")
        route = f"submissions/CIK{cik}.json" if kind == "submissions" else f"api/xbrl/companyfacts/CIK{cik}.json"
        observation = self._get("https://data.sec.gov/" + route)
        data = observation["payload"]
        got = data.get("cik")
        if isinstance(got, bool) or not isinstance(got, (str, int)) or not str(got).isdigit() or int(got) != int(cik):
            raise SecError("IDENTITY_MISMATCH")
        if kind == "submissions":
            tickers = data.get("tickers")
            if not isinstance(tickers, list) or not all(isinstance(t, str) for t in tickers) or canonical_symbol(symbol) not in {canonical_symbol(t) for t in tickers}:
                raise SecError("TICKER_MISMATCH")
            if not isinstance(data.get("filings"), dict) or not isinstance(data["filings"].get("recent"), dict):
                raise SecError("FILINGS_SCHEMA_UNAVAILABLE")
        elif not isinstance(data.get("facts"), dict):
            raise SecError("FACTS_SCHEMA_UNAVAILABLE")
        return observation
