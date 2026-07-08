#!/usr/bin/env python3
"""
kite_token_totp.py — headless daily Kite access-token refresh (no browser).

Automates Zerodha's login (password + TOTP 2FA) and writes a fresh access_token
into configs/kite_config.json, so an unattended cloud VM can trade during IST
market hours without a human present. Run this ~08:45 IST daily (Zerodha
invalidates pre-dawn tokens — see project_kite_token_timing).

SECRETS (never commit) — read from environment / EnvironmentFile:
  KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET
Optional: KITE_CONFIG (default configs/kite_config.json)

The Kite app's redirect_url can be anything reachable-or-not (e.g. https://127.0.0.1);
the request_token is captured from the redirect chain before any page loads.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from kiteconnect import KiteConnect

_LOGIN = "https://kite.zerodha.com/api/login"
_TWOFA = "https://kite.zerodha.com/api/twofa"


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"[TOKEN] FATAL: missing env {name} (set it in the EnvironmentFile)")
    return v


def _extract_request_token(session: requests.Session, kite: KiteConnect) -> str | None:
    """Hit the connect login URL; the request_token rides the redirect to redirect_url."""
    urls: list[str] = []
    try:
        resp = session.get(kite.login_url(), allow_redirects=True, timeout=15)
        urls.append(resp.url)
        urls += [h.headers.get("location", "") for h in resp.history]
    except requests.exceptions.RequestException as exc:  # redirect_url unreachable is fine
        req = getattr(exc, "request", None)
        if req is not None:
            urls.append(getattr(req, "url", ""))
    for u in urls:
        tok = parse_qs(urlparse(u).query).get("request_token")
        if tok:
            return tok[0]
    return None


def main() -> int:
    api_key, api_secret = _env("KITE_API_KEY"), _env("KITE_API_SECRET")
    user_id, password = _env("KITE_USER_ID"), _env("KITE_PASSWORD")
    totp_secret = _env("KITE_TOTP_SECRET")

    s = requests.Session()
    r = s.post(_LOGIN, data={"user_id": user_id, "password": password}, timeout=15)
    r.raise_for_status()
    request_id = r.json()["data"]["request_id"]

    r2 = s.post(_TWOFA, data={
        "user_id": user_id, "request_id": request_id,
        "twofa_value": pyotp.TOTP(totp_secret).now(), "twofa_type": "totp",
    }, timeout=15)
    r2.raise_for_status()

    kite = KiteConnect(api_key=api_key)
    request_token = _extract_request_token(s, kite)
    if not request_token:
        sys.exit("[TOKEN] FATAL: no request_token (check credentials / TOTP clock / redirect_url)")

    access_token = kite.generate_session(request_token, api_secret=api_secret)["access_token"]

    cfg_path = Path(os.environ.get("KITE_CONFIG", "configs/kite_config.json"))
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    cfg.update({"api_key": api_key, "api_secret": api_secret, "access_token": access_token})
    cfg_path.write_text(json.dumps(cfg, indent=2))
    print(f"[TOKEN] OK — fresh access_token (len={len(access_token)}) written to {cfg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
