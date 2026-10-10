"""
FlashScore (RapidAPI "flashscore4") istemcisi.

Kullanım amacı:
  1) Süper Lig (football-data.org'da YOK) maçlarını/fikstürünü çekip, diğer
     turnuvalarla aynı iç formata (football-data.org'a benzer dict) çevirmek
     -- böylece export_excel.py Süper Lig'i de diğer liglerle aynı pipeline'da
     (Poisson + Elo) işleyebilir.
  2) Yaklaşan maçlar için EK bilgi: hakem, stadyum/şehir, (yakında) kadro.

ÖNEMLİ -- API limiti: RapidAPI ücretsiz planı ayda sadece 500 istek
veriyor (x-ratelimit-requests-limit header'ından görülüyor). Bu yüzden:
  - Her cevap diske cache'lenir (football_data_client.py'deki gibi).
  - Maç detayı/kadro gibi "zenginleştirme" çağrıları SADECE yakın zamanda
    (ENRICHMENT_WINDOW_HOURS içinde) başlayacak, en fazla ENRICHMENT_MAX_MATCHES
    kadar maç için yapılır (config.py).
"""
import hashlib
import json
import os
import time
import unicodedata
from typing import Any, Dict, List, Optional

import requests

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

BASE_URL = "https://flashscore4.p.rapidapi.com/api/flashscore/v2"


class FlashScoreError(RuntimeError):
    pass


def normalize_name(name: str) -> str:
    """Takım adı karşılaştırması için: aksan/Türkçe karakterleri sadeleştir, küçük harfe çevir."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    ascii_name = ascii_name.lower()
    for ch in [".", "-", "'"]:
        ascii_name = ascii_name.replace(ch, " ")
    return " ".join(ascii_name.split())


class FlashScoreClient:
    def __init__(self, api_key: Optional[str] = None, cache_dir: Optional[str] = None):
        self.api_key = api_key or config.RAPIDAPI_KEY
        self.cache_dir = cache_dir or config.CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------- cache ----------

    def _cache_path(self, endpoint: str, params: Dict[str, Any]) -> str:
        key_raw = endpoint + json.dumps(params, sort_keys=True)
        key = hashlib.sha256(key_raw.encode()).hexdigest()[:24]
        safe = endpoint.strip("/").replace("/", "_")
        return os.path.join(self.cache_dir, f"fs_{safe}_{key}.json")

    def _read_cache(self, path: str, ttl_hours: float) -> Optional[Any]:
        if not os.path.exists(path):
            return None
        age_hours = (time.time() - os.path.getmtime(path)) / 3600
        if age_hours > ttl_hours:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_stale_cache(self, path: str) -> Optional[Any]:
        """TTL'i hiçe sayıp diskte her ne varsa onu döndürür -- 429 (kota
        aşımı) durumunda tamamen boş dönmek yerine elimizdeki en son bilinen
        veriyle devam etmek için kullanılır."""
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_cache(self, path: str, data: Any) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    # ---------- asıl istek ----------

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, ttl_hours: float = 6.0) -> Any:
        params = params or {}
        cache_path = self._cache_path(endpoint, params)
        cached = self._read_cache(cache_path, ttl_hours)
        if cached is not None:
            return cached

        if not self.api_key:
            raise FlashScoreError("RAPIDAPI_KEY tanımlı değil (.env dosyasına ekle).")

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        headers = {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": "flashscore4.p.rapidapi.com",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code == 429:
            # Kota aşıldı -- tekrar denemek işe yaramaz (dakikalık değil, AYLIK
            # kota). Elimizde bu endpoint için eski (TTL'i geçmiş) bir cache
            # varsa onu kullan; hiç yoksa ancak o zaman hata ver. Böylece bir
            # lig TAMAMEN kaybolmak yerine en son bilinen veriyle (biraz bayat
            # da olsa) rapora girer.
            stale = self._read_stale_cache(cache_path)
            if stale is not None:
                return stale
            raise FlashScoreError("FlashScore/RapidAPI istek limiti aşıldı (429) ve bu veri için hiç cache yok.")
        if resp.status_code != 200:
            raise FlashScoreError(f"FlashScore isteği başarısız: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        self._write_cache(cache_path, data)
        return data

    # ---------- Süper Lig ----------

    def get_super_lig_results(self, max_pages: int = 2) -> List[dict]:
        out = []
        for page in range(1, max_pages + 1):
            data = self.get(
                "tournaments/results",
                {
                    "tournament_template_id": config.SUPER_LIG["tournament_template_id"],
                    "season_id": config.SUPER_LIG["season_id"],
                    "page": page,
                },
                # Bitmiş maç sonuçları artık DEĞİŞMEZ -- uzun TTL ile cache'lenir
                # ki aynı gün içindeki art arda çalıştırmalar bu veriyi boşuna
                # tekrar RapidAPI'den çekip aylık kotayı eritmesin.
                ttl_hours=config.FLASHSCORE_RESULTS_TTL_HOURS,
            )
            if not data:
                break
            out.extend(data)
        return out

    def get_super_lig_fixtures(self, max_pages: int = 2) -> List[dict]:
        out = []
        for page in range(1, max_pages + 1):
            data = self.get(
                "tournaments/fixtures",
                {
                    "tournament_template_id": config.SUPER_LIG["tournament_template_id"],
                    "season_id": config.SUPER_LIG["season_id"],
                    "page": page,
                },
                ttl_hours=6.0,
            )
            if not data:
                break
            out.extend(data)
        return out

    # ---------- jenerik lig (Süper Lig DIŞINDAKİ ek ligler) ----------

    def get_tournament_results(self, tournament_template_id: str, season_id: str, max_pages: int = 2) -> List[dict]:
        """Süper Lig'deki get_super_lig_results ile aynı mantık, ama herhangi
        bir lig için (config.FLASHSCORE_EXTRA_LEAGUES'teki template_id/season_id ile)."""
        out = []
        for page in range(1, max_pages + 1):
            data = self.get(
                "tournaments/results",
                {"tournament_template_id": tournament_template_id, "season_id": season_id, "page": page},
                # bkz. get_super_lig_results -- bitmiş maçlar değişmez, uzun TTL.
                ttl_hours=config.FLASHSCORE_RESULTS_TTL_HOURS,
            )
            if not data:
                break
            out.extend(data)
        return out

    def get_tournament_fixtures(self, tournament_template_id: str, season_id: str, max_pages: int = 2) -> List[dict]:
        out = []
        for page in range(1, max_pages + 1):
            data = self.get(
                "tournaments/fixtures",
                {"tournament_template_id": tournament_template_id, "season_id": season_id, "page": page},
                ttl_hours=6.0,
            )
            if not data:
                break
            out.extend(data)
        return out

    # ---------- maç detayı / kadro / arama ----------

    def get_match_details(self, match_id: str) -> Optional[dict]:
        try:
            return self.get("matches/details", {"match_id": match_id}, ttl_hours=3.0)
        except FlashScoreError:
            return None

    def get_match_lineups(self, match_id: str) -> Optional[list]:
        try:
            return self.get("matches/match/lineups", {"match_id": match_id}, ttl_hours=1.0)
        except FlashScoreError:
            return None

    def get_matches_by_date(self, date_iso: str, timezone: str = "Europe/Istanbul") -> List[dict]:
        """O güne ait TÜM turnuvalar/maçlar (dünya çapında). Büyük bir cevap;
        sadece ihtiyaç duyulan lig/takım eşleştirmesi için kullanılır.

        ÖNEMLİ -- TTL kısa (6 dakika) tutuluyor: bu çağrı CANLI maç durumu
        (collect_live_gap_matches) için kullanılıyor, yani uzun bir cache
        maçın saati geçtiği halde 'Planlandı' görünmesine ya da skorun
        güncellenmemesine yol açar. Kısa TTL'in RapidAPI kotasına etkisi
        küçük: run içinde tüm ligler bu cache'i PAYLAŞIR (tek ağ çağrısı),
        sadece ayrı run'lar arasında (örn. kullanıcı art arda birkaç dakika
        içinde yeniden çalıştırırsa) tazelenir."""
        return self.get(
            "matches/list-by-date",
            {"date": date_iso, "sport_id": 1, "timezone": timezone},
            ttl_hours=0.1,
        )

    def get_top_scorers(self, competition_code: str) -> Optional[list]:
        """Bir turnuvanın gol kralları listesi -- 'kilit oyuncu kadroda yok mu'
        kontrolü için. Liste değişmediği için uzun TTL (24 saat) ile cache'lenir."""
        info = config.FLASHSCORE_TOURNAMENTS.get(competition_code)
        if not info:
            return None
        try:
            return self.get(
                "tournaments/standings/top-scorers",
                {"tournament_id": info["tournament_id"], "tournament_stage_id": info["tournament_stage_id"]},
                ttl_hours=24.0,
            )
        except FlashScoreError:
            return None


def summarize_lineup_side(side: dict, max_names: int = 11) -> str:
    """Bir takım tarafı için kısa kadro özeti. Resmi kadro (startingLineups)
    varsa 'Kesin' etiketiyle, yoksa predictedLineups'tan 'Tahmini' etiketiyle
    döner -- hangisi olduğu HER ZAMAN açıkça yazılır."""
    confirmed = side.get("startingLineups") or []
    predicted = side.get("predictedLineups") or []
    players = confirmed if confirmed else predicted
    label = "Kesin" if confirmed else ("Tahmini" if predicted else None)
    if not players or label is None:
        return "-"
    names = [p.get("fieldName") or p.get("name") or "?" for p in players[:max_names]]
    return f"({label}) " + ", ".join(names)


def _name_tokens(name: str) -> set:
    return {t for t in normalize_name(name).split() if len(t) >= 3}


def key_scorer_names(top_scorers: list, team_name: str, top_n: int = 2) -> List[str]:
    """Bir takımın ligdeki en golcü top_n oyuncusunun adlarını döndürür."""
    nt = normalize_name(team_name)
    names = []
    for row in top_scorers or []:
        rt = normalize_name(row.get("team_name", ""))
        if rt and (rt in nt or nt in rt):
            names.append(row.get("player_name", ""))
        if len(names) >= top_n:
            break
    return names


def player_missing_from_lineup(side: dict, player_name: str) -> bool:
    """side: lineups endpoint'inden bir takımın tarafı (home/away).
    Kadro (kesin ya da tahmini) varsa ve player_name o 11'de YOKSA True döner.
    Kadro hiç yoksa (henüz açıklanmamış) None/False -- bilinmiyor demektir."""
    confirmed = side.get("startingLineups") or []
    predicted = side.get("predictedLineups") or []
    players = confirmed if confirmed else predicted
    if not players:
        return False  # kadro bilinmiyor -- "eksik" diyemeyiz

    target_tokens = _name_tokens(player_name)
    if not target_tokens:
        return False
    for p in players:
        cand_tokens = _name_tokens(p.get("name", "")) | _name_tokens(p.get("fieldName", ""))
        if target_tokens & cand_tokens:
            return False  # oyuncu kadroda bulundu
    return True


def summarize_missing_players(side: dict) -> str:
    missing = side.get("missingPlayers") or []
    unsure = side.get("unsureMissingPlayers") or []
    parts = []
    if missing:
        parts.append(", ".join((p.get("name") or "?") + (f" [{p.get('reason')}]" if p.get("reason") else "") for p in missing))
    if unsure:
        parts.append("Belirsiz: " + ", ".join(p.get("name") or "?" for p in unsure))
    return " | ".join(parts) if parts else "-"


# ---------- normalize: FlashScore maçını football-data.org benzeri dict'e çevir ----------

def _fs_timestamp_to_iso(ts: int) -> str:
    import datetime as dt
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def fs_match_to_fd_shape(m: dict, competition_code: str, competition_name: str, is_result: bool) -> dict:
    """FlashScore 'tournaments/results' veya 'tournaments/fixtures' kaydını
    football-data.org maç dict şekline çevirir (export_excel.py bu şekli bekliyor)."""
    scores = m.get("scores") or {}
    home_g = scores.get("home")
    away_g = scores.get("away")
    if is_result and home_g is not None and away_g is not None:
        status = "FINISHED"
    else:
        status = "SCHEDULED"

    return {
        "id": f"fs_{m['match_id']}",
        "flashscore_match_id": m["match_id"],
        "utcDate": _fs_timestamp_to_iso(m["timestamp"]),
        "status": status,
        "score": {
            "fullTime": {"home": home_g, "away": away_g},
            "halfTime": {"home": None, "away": None},
        },
        "homeTeam": {"name": m["home_team"]["name"]},
        "awayTeam": {"name": m["away_team"]["name"]},
        "competition": {"code": competition_code, "name": competition_name},
    }


def fs_live_match_to_fd_shape(m: dict, competition_code: str, competition_name: str) -> dict:
    """'tournaments/results' (SADECE bitmiş maçlar) ve 'tournaments/fixtures'
    (SADECE henüz başlamamış maçlar) uç noktaları, o anda CANLI oynanan bir
    maçı HİÇ döndürmüyor -- ikisinin arasında kalıyor. Bu fonksiyon,
    matches/list-by-date'ten (bkz. get_matches_by_date) gelen bir maç kaydını
    aynı football-data.org benzeri şekle çevirir, böylece o boşluk kapanır."""
    status_info = m.get("match_status") or {}
    scores = m.get("scores") or {}

    if status_info.get("is_cancelled"):
        status = "CANCELLED"
    elif status_info.get("is_postponed"):
        status = "POSTPONED"
    elif status_info.get("is_finished") or status_info.get("is_finished_after_extra_time") or status_info.get("is_finished_after_penalties"):
        status = "FINISHED"
    elif status_info.get("is_in_progress"):
        stage = (status_info.get("stage") or "").lower()
        status = "PAUSED" if "half time" in stage else "IN_PLAY"
    else:
        status = "SCHEDULED"

    return {
        "id": f"fs_{m['match_id']}",
        "flashscore_match_id": m["match_id"],
        "utcDate": _fs_timestamp_to_iso(m["timestamp"]),
        "status": status,
        "score": {
            "fullTime": {"home": scores.get("home"), "away": scores.get("away")},
            "halfTime": {"home": None, "away": None},
        },
        "homeTeam": {"name": m["home_team"]["name"]},
        "awayTeam": {"name": m["away_team"]["name"]},
        "competition": {"code": competition_code, "name": competition_name},
    }


def find_match_id_by_teams(day_tournaments: List[dict], home: str, away: str) -> Optional[str]:
    """get_matches_by_date() çıktısında, verilen ev/deplasman adlarına en yakın
    eşleşen maçın flashscore match_id'sini döndürür (basit normalize + alt-dize eşleşmesi)."""
    nh, na = normalize_name(home), normalize_name(away)
    for t in day_tournaments:
        for mt in t.get("matches", []):
            h = normalize_name(mt["home_team"]["name"])
            a = normalize_name(mt["away_team"]["name"])
            if (nh in h or h in nh) and (na in a or a in na):
                return mt["match_id"]
    return None
