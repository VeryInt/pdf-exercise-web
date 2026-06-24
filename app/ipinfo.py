from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.config import settings
from app.db import get_ip_geo, upsert_ip_geo


UNKNOWN_GEO = {
    "asn": "",
    "as_name": "",
    "as_domain": "",
    "country_code": "",
    "country": "",
    "continent_code": "",
    "continent": "",
}


def cache_is_fresh(record: dict, days: int) -> bool:
    try:
        updated_at = datetime.fromisoformat(str(record["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    return updated_at >= datetime.now(timezone.utc) - timedelta(days=days)


def is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def lookup_ip_geo(ip: str) -> dict:
    cached = get_ip_geo(ip)
    if cached and cache_is_fresh(cached, settings.ipinfo_cache_days):
        return cached

    if not is_public_ip(ip):
        return upsert_ip_geo(ip, UNKNOWN_GEO, lookup_status="non_public")

    token = settings.ipinfo_token.strip()
    if not token:
        if cached:
            return cached
        return {**UNKNOWN_GEO, "ip": ip, "lookup_status": "not_configured"}

    request = Request(
        f"https://api.ipinfo.io/lite/{quote(ip, safe='')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "pdf-exercise-web/1.0",
        },
    )
    try:
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("IPInfo response was not an object")
        return upsert_ip_geo(ip, payload, lookup_status="ok")
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        if cached:
            return cached
        return upsert_ip_geo(ip, UNKNOWN_GEO, lookup_status="failed")


def lookup_ip_geos(ips: list[str]) -> None:
    token = settings.ipinfo_token.strip()
    if not token:
        return

    pending: list[str] = []
    for ip in dict.fromkeys(ips):
        cached = get_ip_geo(ip)
        if cached and cache_is_fresh(cached, settings.ipinfo_cache_days):
            continue
        if not is_public_ip(ip):
            upsert_ip_geo(ip, UNKNOWN_GEO, lookup_status="non_public")
            continue
        pending.append(ip)

    if not pending:
        return

    request = Request(
        "https://api.ipinfo.io/batch/lite",
        data=json.dumps(pending[:1000]).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "pdf-exercise-web/1.0",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return
        for ip in pending:
            data = payload.get(ip)
            if isinstance(data, dict):
                upsert_ip_geo(ip, data, lookup_status="ok")
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return
