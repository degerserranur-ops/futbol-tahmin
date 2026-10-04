"""
Open-Meteo (ücretsiz, API key gerektirmiyor) ile şehir bazlı hava durumu.

Akış: şehir adı -> geocoding (lat/lon) -> o koordinat + maç saati için
saatlik hava durumu tahmini. Sonuçlar diske cache'lenir (geocoding uzun süre
sabit kalır; hava tahmini kısa TTL ile).
"""
import hashlib
import json
import os
import time
from typing import Optional, Tuple

import requests

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

WEATHER_CODE_TR = {
    0: "Açık", 1: "Az bulutlu", 2: "Parçalı bulutlu", 3: "Bulutlu",
    45: "Sisli", 48: "Kırağı sisi",
    51: "Hafif çisenti", 53: "Çisenti", 55: "Yoğun çisenti",
    61: "Hafif yağmur", 63: "Yağmur", 65: "Şiddetli yağmur",
    71: "Hafif kar", 73: "Kar", 75: "Yoğun kar",
    80: "Sağanak", 81: "Sağanak", 82: "Şiddetli sağanak",
    95: "Gök gürültülü sağanak", 96: "Dolu ile fırtına", 99: "Şiddetli dolu fırtınası",
}


def _cache_path(cache_dir: str, prefix: str, key_raw: str) -> str:
    key = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
    return os.path.join(cache_dir, f"{prefix}_{key}.json")


def _read_cache(path: str, ttl_hours: float):
    if not os.path.exists(path):
        return None
    age_hours = (time.time() - os.path.getmtime(path)) / 3600
    if age_hours > ttl_hours:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_cache(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def geocode_city(city: str, cache_dir: Optional[str] = None) -> Optional[Tuple[float, float]]:
    if not city:
        return None
    cache_dir = cache_dir or config.CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, "geo", city.lower())
    cached = _read_cache(path, ttl_hours=24 * 30)  # şehir koordinatı ayda bir güncellense yeter
    if cached is not None:
        return tuple(cached) if cached else None

    try:
        resp = requests.get(
            config.OPEN_METEO_GEOCODE_URL,
            params={"name": city, "count": 1, "language": "tr", "format": "json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if not results:
            _write_cache(path, None)
            return None
        lat, lon = results[0]["latitude"], results[0]["longitude"]
        _write_cache(path, [lat, lon])
        return lat, lon
    except Exception:
        return None


def get_weather_at(city: str, iso_utc_datetime: str, cache_dir: Optional[str] = None) -> Optional[str]:
    """Verilen şehir + UTC zaman için kısa bir Türkçe hava durumu özeti döndürür
    (örn. '14°C, Parçalı bulutlu, 12 km/sa rüzgar'). Bulunamazsa None."""
    coords = geocode_city(city, cache_dir=cache_dir)
    if coords is None:
        return None
    lat, lon = coords

    cache_dir = cache_dir or config.CACHE_DIR
    date_part = iso_utc_datetime[:10]
    path = _cache_path(cache_dir, "wx", f"{lat}_{lon}_{date_part}")
    data = _read_cache(path, ttl_hours=3.0)
    if data is None:
        try:
            resp = requests.get(
                config.OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m,precipitation_probability,weathercode,windspeed_10m",
                    "timezone": "UTC",
                    "start_date": date_part,
                    "end_date": date_part,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            _write_cache(path, data)
        except Exception:
            return None

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return None
    target_hour = iso_utc_datetime[:13]  # "YYYY-MM-DDTHH"
    idx = None
    for i, t in enumerate(times):
        if t.startswith(target_hour):
            idx = i
            break
    if idx is None:
        return None

    try:
        temp = hourly["temperature_2m"][idx]
        code = hourly["weathercode"][idx]
        wind = hourly["windspeed_10m"][idx]
        desc = WEATHER_CODE_TR.get(int(code), "-")
        return f"{round(temp)}°C, {desc}, {round(wind)} km/sa"
    except Exception:
        return None
