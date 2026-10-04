"""
football-data.org (v4) için basit, cache'li istemci.

API-Football'dan farkı: tek bir istekle bir turnuvanın İSTEDİĞİN tarih
aralığındaki tüm maçlarını (geçmiş sonuçlar + gelecek fikstür) çekebiliyoruz
(dateFrom/dateTo parametreleri) - gün gün dolaşmaya gerek yok. Dakikalık
istek limiti olduğu için (~10/dk) cevaplar diske cache'lenir.
"""
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

import requests

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class FootballDataError(RuntimeError):
    pass


class FootballDataClient:
    def __init__(self, token: Optional[str] = None, cache_dir: Optional[str] = None):
        self.token = token or config.FOOTBALL_DATA_TOKEN
        self.cache_dir = cache_dir or config.CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------- cache yardımcıları ----------

    def _cache_path(self, endpoint: str, params: Dict[str, Any]) -> str:
        key_raw = endpoint + json.dumps(params, sort_keys=True)
        key = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
        safe_endpoint = endpoint.strip("/").replace("/", "_")
        return os.path.join(self.cache_dir, f"fd_{safe_endpoint}_{key}.json")

    def _read_cache(self, path: str, ttl_hours: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not os.path.exists(path):
            return None
        ttl = config.CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
        if ttl is not None:
            age_hours = (time.time() - os.path.getmtime(path)) / 3600
            if age_hours > ttl:
                return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_cache(self, path: str, data: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    # ---------- asıl istek ----------

    def get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        use_cache: bool = True,
        ttl_hours: Optional[float] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        cache_path = self._cache_path(endpoint, params)

        if use_cache:
            cached = self._read_cache(cache_path, ttl_hours=ttl_hours)
            if cached is not None:
                return cached

        if not self.token:
            raise FootballDataError(
                "FOOTBALL_DATA_TOKEN tanımlı değil. .env dosyasına FOOTBALL_DATA_TOKEN=... ekle "
                "(bkz. config.py üstündeki açıklama)."
            )

        url = f"{config.FOOTBALL_DATA_BASE_URL}/{endpoint.lstrip('/')}"
        headers = {"X-Auth-Token": self.token}

        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code == 429:
            raise FootballDataError("Dakikalık istek limiti aşıldı (429). Biraz bekleyip tekrar dene.")
        if resp.status_code != 200:
            raise FootballDataError(f"API isteği başarısız: {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        self._write_cache(cache_path, data)
        return data

    # ---------- üst seviye yardımcı metotlar ----------

    def get_matches(self, competition_code: str, date_from: str, date_to: str) -> list:
        """
        Bir turnuvanın verilen tarih aralığındaki TÜM maçlarını döndürür
        (biten + planlanan). Free tier'da aralık genelde en fazla ~10 gün
        olabilir; export_excel.py gerekirse aralığı parçalara bölerek çağırır.
        """
        params = {"dateFrom": date_from, "dateTo": date_to}
        data = self.get(f"competitions/{competition_code}/matches", params)
        return data.get("matches", [])
