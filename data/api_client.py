"""
API-Football için basit, cache'li istemci.

Ücretsiz plan günlük 100 istekle sınırlı olduğundan, her cevap
cache/ klasörüne JSON olarak yazılır ve CACHE_TTL_HOURS süresi
içinde aynı istek tekrar API'ye gönderilmez; diskten okunur.
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


class ApiFootballError(RuntimeError):
    pass


class ApiFootballClient:
    def __init__(self, api_key: Optional[str] = None, cache_dir: Optional[str] = None):
        self.api_key = api_key or config.API_FOOTBALL_KEY
        self.cache_dir = cache_dir or config.CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------- cache yardımcıları ----------

    def _cache_path(self, endpoint: str, params: Dict[str, Any]) -> str:
        key_raw = endpoint + json.dumps(params, sort_keys=True)
        key = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
        safe_endpoint = endpoint.strip("/").replace("/", "_")
        return os.path.join(self.cache_dir, f"{safe_endpoint}_{key}.json")

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

        if not self.api_key:
            raise ApiFootballError(
                "API_FOOTBALL_KEY tanımlı değil. .env dosyasına API_FOOTBALL_KEY=... ekle "
                "(bkz. config.py üstündeki açıklama)."
            )

        url = f"{config.API_FOOTBALL_BASE_URL}/{endpoint.lstrip('/')}"
        headers = {"x-apisports-key": self.api_key}

        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            raise ApiFootballError(f"API isteği başarısız: {resp.status_code} {resp.text[:300]}")

        data = resp.json()
        if data.get("errors"):
            raise ApiFootballError(f"API hata döndürdü: {data['errors']}")

        self._write_cache(cache_path, data)
        return data

    # ---------- üst seviye yardımcı metotlar ----------

    def get_fixtures(self, league_id: int, season: int, status: Optional[str] = None) -> list:
        """Bir lig/sezon için tüm maçları döndürür (biten + planlanan)."""
        params = {"league": league_id, "season": season}
        if status:
            params["status"] = status
        data = self.get("fixtures", params)
        return data.get("response", [])

    def get_upcoming_fixtures(self, league_id: int, season: int, next_n: int = 10) -> list:
        params = {"league": league_id, "season": season, "next": next_n}
        data = self.get("fixtures", params)
        return data.get("response", [])

    def get_fixtures_by_date(self, date_str: str, is_past: bool = False) -> list:
        """
        Tek bir takvim günündeki TÜM dünya maçlarını döndürür (tüm ligler).
        Free plan'da league+season birlikte verilen toplu istekler sezon
        kısıtlamasına çarpıyor (bkz. README); ama saf 'date' sorgusu bu
        kısıtlamaya girmiyor ve güncel sezonu da döndürüyor.

        is_past=True ise cache süresiz tutulur (biten maçın sonucu değişmez,
        bu yüzden günlük istek kotasını boşa harcamayalım).
        """
        ttl = None if is_past else config.CACHE_TTL_HOURS
        data = self.get("fixtures", {"date": date_str}, ttl_hours=ttl)
        return data.get("response", [])

    def get_team_id(self, name: str, league_id: int, season: int) -> Optional[int]:
        """İsme göre, verilen ligdeki takımın ID'sini bulur (basit eşleştirme)."""
        data = self.get("teams", {"league": league_id, "season": season})
        name_lower = name.strip().lower()
        for item in data.get("response", []):
            team = item.get("team", {})
            if name_lower in team.get("name", "").lower():
                return team.get("id")
        return None
