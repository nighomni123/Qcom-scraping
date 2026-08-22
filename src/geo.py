"""
geo.py — Mumbai corridor (Virar -> Andheri) and dark-store resolution.

Quick-commerce prices are PER DARK STORE. A glitch in Andheri often does not
exist in Borivali. So we anchor each crawl to a real station coordinate, which
makes the app resolve a *different* store, and we treat each (store, sku) as
its own baseline.

This module is pure geometry + a pluggable resolver. The resolver talks to each
app's location endpoint to turn lat/lon into a store_id. When the network blocks
that (common), we fall back to a deterministic synthetic store_id derived from
the coordinate so the pipeline can still run and label data by location.
"""
from __future__ import annotations

import math
import urllib.request
import json

# Haversine for "nearest store" ranking if we get multiple candidates.
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class Corridor:
    """Ordered list of stations from config with lat/lon."""

    def __init__(self, stations):
        self.stations = stations  # list of dicts {station, lat, lon}

    def anchors(self):
        return [(s["station"], s["lat"], s["lon"]) for s in self.stations]

    def nearest(self, lat: float, lon: float):
        best = min(self.stations, key=lambda s: haversine_km(lat, lon, s["lat"], s["lon"]))
        return best["station"], haversine_km(lat, lon, best["lat"], best["lon"])


# Per-app location endpoints. Paths rotate; we try a small set and the adapter
# can override. These are best-effort and expected to fail on a server — that's
# why we have a deterministic fallback store_id.
LOCATION_ENDPOINTS = {
    "blinkit": "https://blinkit.com/api/v4/location/page?lat={lat}&lon={lon}",
    "instamart": "https://www.swiggy.com/api/instamart/v2/location?lat={lat}&lon={lon}",
    "zepto": "https://www.zeptonow.com/api/v2/get_locality?lat={lat}&lon={lon}",
}


def resolve_store(app: str, station: str, lat: float, lon: float, timeout=8):
    """
    Returns (store_id, store_label). Tries the live endpoint; on any failure
    returns a synthetic but stable id so downstream labeling still works.
    """
    synth = f"{app}::{station}@{lat:.3f},{lon:.3f}"
    url = LOCATION_ENDPOINTS.get(app)
    if not url:
        return synth, f"{station} (synthetic)"
    try:
        req = urllib.request.Request(
            url.format(lat=lat, lon=lon),
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        # Endpoints differ; pull a plausible id/name defensively.
        sid = (
            data.get("store_id")
            or data.get("id")
            or (data.get("data") or {}).get("store_id")
            or synth
        )
        label = data.get("name") or data.get("store_name") or station
        return str(sid), f"{label} ({station})"
    except Exception:
        return synth, f"{station} (synthetic)"


if __name__ == "__main__":
    c = Corridor([
        {"station": "Virar", "lat": 19.456, "lon": 72.806},
        {"station": "Andheri", "lat": 19.119, "lon": 72.846},
    ])
    print("nearest to 19.2,72.85:", c.nearest(19.2, 72.85))
    print("resolve blinkit@Andheri:", resolve_store("blinkit", "Andheri", 19.119, 72.846))
