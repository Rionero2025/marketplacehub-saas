from __future__ import annotations

import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from services.db import DATA_DIR

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_CACHE = DATA_DIR / "fx_rates.json"
# Used only on the very first offline start; a successful request replaces it.
BOOTSTRAP = {"date": "2026-07-20", "rates": {"PLN": 4.3320, "CZK": 24.1900}}


def _parse_ecb_xml(content: bytes) -> dict:
    root = ET.fromstring(content)
    date = ""
    rates: dict[str, float] = {}
    for element in root.iter():
        if element.attrib.get("time"):
            date = element.attrib["time"]
        currency = element.attrib.get("currency")
        rate = element.attrib.get("rate")
        if currency and rate:
            rates[currency.upper()] = float(rate)
    if not date or not all(code in rates for code in ("PLN", "CZK")):
        raise ValueError("La risposta BCE non contiene i cambi PLN e CZK.")
    return {"date": date, "rates": rates}


def get_ecb_rates() -> dict:
    """Return ECB EUR reference rates, falling back to the last saved response."""
    try:
        request = urllib.request.Request(
            ECB_DAILY_URL,
            headers={"User-Agent": "MarketplaceHub/1.0 (+currency conversion)"},
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            result = _parse_ecb_xml(response.read())
        result.update({"online": True, "source": "Banca Centrale Europea"})
        FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FX_CACHE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result
    except Exception as error:
        cached = BOOTSTRAP.copy()
        if FX_CACHE.exists():
            try:
                cached = json.loads(FX_CACHE.read_text(encoding="utf-8"))
            except Exception:
                pass
        cached.update({
            "online": False,
            "source": "Ultimo cambio BCE salvato",
            "warning": str(error),
        })
        return cached
