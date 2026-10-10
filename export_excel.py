"""
Belirli bir tarih aralığındaki Avrupa ligi maçlarını (oynanmış + oynanacak)
tek bir Excel tablosunda toplar: tarih, gün, saat, lig, ev sahibi,
deplasman, İY skor, MS skor, durum ve -- yeterli geçmiş veri varsa --
model tahmini (1X2 olasılıkları, beklenen skor, 2.5 üst/alt, KG var/yok).

Veri kaynağı: football-data.org (v4). Süper Lig bu kaynakta YOK; config.py
içindeki COMPETITIONS'da sadece free tier'da sonsuza dek ücretsiz olan
Avrupa turnuvaları var (Premier League, La Liga, Serie A, Bundesliga,
Ligue 1, Şampiyonlar Ligi).

Free tier'da dateFrom/dateTo aralığı tek istekte en fazla ~10 gün olabildiği
için, istenen aralık 10 günlük parçalara bölünüp sırayla çekilir (dakikalık
limite karşı aralarda kısa bekleme eklenir).

Kullanım:
  python export_excel.py --lookback-days 40 --forward-days 10
  python export_excel.py --competitions premier_league la_liga
"""
import argparse
import datetime as dt
import time
from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import config
from data.football_data_client import FootballDataClient, FootballDataError
from data.flashscore_client import (
    FlashScoreClient, FlashScoreError, fs_match_to_fd_shape, fs_live_match_to_fd_shape,
    find_match_id_by_teams, summarize_lineup_side, key_scorer_names, player_missing_from_lineup,
    normalize_name,
)
from data.weather_client import get_weather_at
from data import history_store
from models.elo import EloRatings
from models.poisson_model import MatchResult, PoissonGoalModel

ISTANBUL = ZoneInfo("Europe/Istanbul")

GUN_ADLARI = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

DURUM_MAP = {
    "FINISHED": "Bitti",
    "SCHEDULED": "Planlandı",
    "TIMED": "Planlandı",
    "IN_PLAY": "Canlı",
    "PAUSED": "Devre Arası",
    "POSTPONED": "Ertelendi",
    "SUSPENDED": "Durduruldu",
    "CANCELLED": "İptal",
    "AWARDED": "Hükmen",
}

ULKE_MAP = {
    "PL": "İngiltere",
    "PD": "İspanya",
    "SA": "İtalya",
    "BL1": "Almanya",
    "FL1": "Fransa",
    "CL": "Avrupa",
    "SUPER_LIG": "Türkiye",
    "DED": "Hollanda",
    "PPL": "Portekiz",
    "BSA": "Brezilya",
    "TFF1": "Türkiye",
    "JPL": "Belçika",
    "ARG": "Arjantin",
    "UEL": "Avrupa",
    "UECL": "Avrupa",
    "ELC": "İngiltere",
    "BL2": "Almanya",
    "FL2": "Fransa",
    "UNL": "Avrupa",
}

MAX_RANGE_DAYS = 60  # pratikte test edilen aralık; daha büyüğü gerekirse otomatik parçalanır


def chunk_date_range(start: dt.date, end: dt.date, max_days: int = MAX_RANGE_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + dt.timedelta(days=max_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + dt.timedelta(days=1)


def collect_matches(client: FootballDataClient, comp_codes: List[str], start: dt.date, end: dt.date) -> List[dict]:
    collected = []
    for code in comp_codes:
        for c_start, c_end in chunk_date_range(start, end):
            try:
                matches = client.get_matches(code, c_start.isoformat(), c_end.isoformat())
            except FootballDataError as e:
                print(f"  UYARI [{code} {c_start}->{c_end}]: {e}")
                continue
            for m in matches:
                collected.append(m)
            time.sleep(1.2)  # dakikalık limite karşı nazik ol
    return collected


def collect_live_gap_matches(fs: FlashScoreClient, known_teams: set, code: str, name: str, seen: set, existing: List[dict]) -> List[dict]:
    """İki ayrı sorunu çözer:
    1) GERÇEK BOŞLUK: 'tournaments/results' (SADECE bitmiş) + 'tournaments/fixtures'
       (SADECE henüz başlamamış) ikilisi, o anda CANLI oynanan bir maçı hiç
       döndürmüyor -- ikisinin arasında kalıyor.
    2) BAYAT DURUM: 'tournaments/fixtures' 6 saat cache'leniyor, yani bir maç
       çekildiği anda 'SCHEDULED' (Planlandı) damgalanıyor ve maç sonradan
       canlıya geçse/bitse bile cache süresi dolana kadar hâlâ 'Planlandı'
       görünüyor (örn. saati gelmiş ama tabloda hâlâ başlamamış gösteriliyor).
    Her ikisi için de -- ve AYRICA, 'tournaments/results' artık uzun TTL ile
    cache'lendiği için (bkz. config.FLASHSCORE_RESULTS_TTL_HOURS) results
    cache'i bayatken yakın geçmişte biten bir maç hâlâ 'Planlandı' görünüp
    kalabilir -- sadece bugünün değil, son config.LIVE_GAP_LOOKBACK_DAYS
    günün TÜM dünya maçlarını (matches/list-by-date, kısa TTL, run başına
    gün sayısı kadar çağrı -- ligler arası paylaşılır) tarayıp:
      - 'existing' içindeki (zaten results/fixtures'tan gelen) ve hâlâ
        'SCHEDULED' görünen maçların durumunu/skorunu TAZE veriyle günceller
        (yerinde, existing listesindeki dict'leri mutate ederek),
      - 'existing'te hiç olmayan (gerçek boşluk) maçları yeni liste olarak
        döndürür."""
    by_id = {}
    for day_offset in range(config.LIVE_GAP_LOOKBACK_DAYS):
        date_iso = (dt.date.today() - dt.timedelta(days=day_offset)).isoformat()
        try:
            day_data = fs.get_matches_by_date(date_iso)
        except FlashScoreError as e:
            print(f"  UYARI [{name} / canlı maç taraması {date_iso}]: {e}")
            continue
        for t in day_data or []:
            for m in t.get("matches", []):
                by_id[m["match_id"]] = m

    # 1) bayat durumları güncelle (yerinde)
    for mm in existing:
        fid = mm.get("flashscore_match_id")
        if fid in by_id and mm.get("status") == "SCHEDULED":
            fresh = fs_live_match_to_fd_shape(by_id[fid], code, name)
            if fresh["status"] != "SCHEDULED":
                mm["status"] = fresh["status"]
                mm["score"] = fresh["score"]

    # 2) gerçek boşluk maçlarını ekle
    out = []
    for fid, m in by_id.items():
        if fid in seen:
            continue
        h = m["home_team"]["name"]
        a = m["away_team"]["name"]
        if h in known_teams and a in known_teams:
            seen.add(fid)
            out.append(fs_live_match_to_fd_shape(m, code, name))
    return out


def patch_football_data_live_gap(matches: List[dict]) -> None:
    """collect_live_gap_matches'in 'bayat durum' düzeltmesi SADECE FlashScore
    kaynaklı maçlarda çalışır (eşleştirme 'flashscore_match_id' alanına göre).
    football-data.org'dan gelen maçlar (Premier League, La Liga, Serie A,
    Bundesliga, Ligue 1, Eredivisie, Primeira Liga, Brezilya Serie A,
    Championship, UCL) bu düzeltmeden hiç geçmiyordu -- football-data.org
    cache'i (TTL 6 saat, tüm turnuva+tarih aralığı TEK bir cache anahtarı)
    maç başladığı anda değil, cache süresi dolunca tazeleniyor. Sonuç: maç
    fiilen oynanıyor olsa bile tabloda saatlerce 'Planlandı' kalabiliyordu.

    Bu fonksiyon AYNI (zaten paylaşılan, kısa TTL'li) matches/list-by-date
    taramasını kullanıp -- ek ağ isteği GENELDE gerekmez, çünkü bu run
    içinde FlashScore ligleri için zaten çekilmiş oluyor -- hâlâ 'SCHEDULED'
    görünen football-data.org maçlarını takım adına göre (normalize_name ile
    alt-dize eşleştirmesi) bulup durumunu/skorunu yerinde günceller."""
    try:
        fs = FlashScoreClient()
    except Exception:
        return

    covered_dates = set()
    by_id: Dict[str, dict] = {}
    for day_offset in range(config.LIVE_GAP_LOOKBACK_DAYS):
        d = dt.date.today() - dt.timedelta(days=day_offset)
        covered_dates.add(d.isoformat())
        try:
            day_data = fs.get_matches_by_date(d.isoformat())
        except FlashScoreError:
            continue
        for t in day_data or []:
            for m in t.get("matches", []):
                by_id[m["match_id"]] = m
    if not by_id:
        return

    candidates = [
        (normalize_name(m["home_team"]["name"]), normalize_name(m["away_team"]["name"]), m)
        for m in by_id.values()
    ]

    patched = 0
    for mm in matches:
        if mm.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        if "flashscore_match_id" in mm:
            continue  # bu zaten collect_live_gap_matches'ten geçti
        if mm["utcDate"][:10] not in covered_dates:
            continue
        nh = normalize_name(mm["homeTeam"]["name"])
        na = normalize_name(mm["awayTeam"]["name"])
        for h, a, m in candidates:
            if (nh in h or h in nh) and (na in a or a in na):
                fresh = fs_live_match_to_fd_shape(m, mm["competition"]["code"], mm["competition"]["name"])
                if fresh["status"] != "SCHEDULED":
                    mm["status"] = fresh["status"]
                    mm["score"] = fresh["score"]
                    patched += 1
                break
    if patched:
        print(f"  (football-data.org canlı/bitti düzeltmesi: {patched} maç)")


def _merge_with_history(code: str, out: List[dict]) -> List[dict]:
    """Bu çalıştırmada elde edilen 'out' listesindeki BİTMİŞ maçları kalıcı
    tarihçeye (data/history_store.py) işler, ardından training havuzu olarak
    şunu döndürür: tarihçedeki TÜM bilinen bitmiş maçlar + bu run'da gelen
    bitmemiş (Planlandı/canlı) maçlar. Böylece 'results' çağrısı bu run'da
    hiç başarısız olsa bile (out tamamen boş gelse bile), elimizde geçmişte
    biriktirilmiş bitmiş maç havuzu training için hâlâ mevcut olur -- sadece
    o run için YENİ bitmiş maç bilgisi eksik kalır, TÜM geçmiş kaybolmaz."""
    history_store.merge_into_history(code, out)
    combined: Dict[str, dict] = dict(history_store.load_history(code))
    for m in out:
        if m.get("status") != "FINISHED":
            combined[m["id"]] = m
    return list(combined.values())


def collect_super_lig_matches() -> List[dict]:
    """Süper Lig'i football-data.org'da olmadığı için FlashScore'dan çeker ve
    diğer turnuvalarla aynı dict şekline çevirir. Hata olursa (key yok, limit
    aşıldı, vb.) kalıcı tarihçedeki bilinen maçlarla devam eder."""
    fs = FlashScoreClient()
    code = config.SUPER_LIG["code"]
    name = config.SUPER_LIG["name"]
    results, fixtures = [], []
    try:
        results = fs.get_super_lig_results(max_pages=2)
    except FlashScoreError as e:
        print(f"  UYARI [Süper Lig sonuçlar / FlashScore]: {e}")
    try:
        fixtures = fs.get_super_lig_fixtures(max_pages=2)
    except FlashScoreError as e:
        print(f"  UYARI [Süper Lig fikstür / FlashScore]: {e}")

    out = []
    seen = set()
    for m in results:
        if m["match_id"] in seen:
            continue
        seen.add(m["match_id"])
        out.append(fs_match_to_fd_shape(m, code, name, is_result=True))
    for m in fixtures:
        if m["match_id"] in seen:
            continue
        seen.add(m["match_id"])
        out.append(fs_match_to_fd_shape(m, code, name, is_result=False))

    if out:
        known_teams = {mm["homeTeam"]["name"] for mm in out} | {mm["awayTeam"]["name"] for mm in out}
        out.extend(collect_live_gap_matches(fs, known_teams, code, name, seen, out))
    return _merge_with_history(code, out)


def collect_flashscore_extra_league(league_key: str) -> List[dict]:
    """Süper Lig'in dışında, football-data.org'da OLMAYAN ek bir ligi
    (TFF 1. Lig, Jupiler Pro League, Arjantin Liga Profesional, UEFA Avrupa
    Ligi/Konferans Ligi -- bkz. config.FLASHSCORE_EXTRA_LEAGUES) aynı yöntemle
    FlashScore'dan çeker. Hata olursa kalıcı tarihçedeki bilinen maçlarla devam eder."""
    info = config.FLASHSCORE_EXTRA_LEAGUES.get(league_key)
    if not info:
        return []
    fs = FlashScoreClient()
    code = info["code"]
    name = info["name"]
    results, fixtures = [], []
    try:
        results = fs.get_tournament_results(info["tournament_template_id"], info["season_id"], max_pages=2)
    except FlashScoreError as e:
        print(f"  UYARI [{info['name']} sonuçlar / FlashScore]: {e}")
    try:
        fixtures = fs.get_tournament_fixtures(info["tournament_template_id"], info["season_id"], max_pages=2)
    except FlashScoreError as e:
        print(f"  UYARI [{info['name']} fikstür / FlashScore]: {e}")

    out = []
    seen = set()
    for m in results:
        if m["match_id"] in seen:
            continue
        seen.add(m["match_id"])
        out.append(fs_match_to_fd_shape(m, code, name, is_result=True))
    for m in fixtures:
        if m["match_id"] in seen:
            continue
        seen.add(m["match_id"])
        out.append(fs_match_to_fd_shape(m, code, name, is_result=False))

    if out:
        known_teams = {mm["homeTeam"]["name"] for mm in out} | {mm["awayTeam"]["name"] for mm in out}
        out.extend(collect_live_gap_matches(fs, known_teams, code, name, seen, out))
    return _merge_with_history(code, out)


def to_match_result(m: dict) -> Optional[MatchResult]:
    if m["status"] != "FINISHED":
        return None
    ft = m["score"]["fullTime"]
    hg, ag = ft.get("home"), ft.get("away")
    if hg is None or ag is None:
        return None
    # devre arası skoru -- football-data.org kaynaklı maçlarda gerçek veri var,
    # FlashScore kaynaklılarda yok (halfTime hep None); PoissonGoalModel.fit()
    # bunu opsiyonel kullanıyor, yoksa varsayılan orana düşüyor (bkz. poisson_model.py)
    ht = m["score"].get("halfTime") or {}
    hg_ht, ag_ht = ht.get("home"), ht.get("away")
    return MatchResult(
        home=m["homeTeam"]["name"], away=m["awayTeam"]["name"],
        home_goals=hg, away_goals=ag,
        home_goals_ht=hg_ht, away_goals_ht=ag_ht,
        date=m.get("utcDate"),
    )


def build_models_per_competition(matches: List[dict]) -> Dict[str, dict]:
    by_comp: Dict[str, List[dict]] = defaultdict(list)
    for m in matches:
        by_comp[m["competition"]["code"]].append(m)

    models = {}
    for code, ms in by_comp.items():
        results = [r for r in (to_match_result(m) for m in ms) if r is not None]
        if len(results) < config.MIN_MATCHES_FOR_MODEL:
            models[code] = None
            continue
        poisson_model = PoissonGoalModel()
        poisson_model.fit(results)
        elo = EloRatings()
        for r in results:
            elo.update_match(r.home, r.away, r.home_goals, r.away_goals)
        # "results" da saklanıyor: bitmiş bir maçın kendi tahminini üretirken
        # kendi sonucunu eğitimden çıkarabilmek (leave-one-out) için -- bkz.
        # build_rows. Aksi halde bitmiş bir maç, kendi sonucunun katkıda
        # bulunduğu bir modelle tahmin edilmiş olur (hafif, ama gerçek bir
        # kendi-kendini-doğrulama sızıntısı).
        models[code] = {
            "poisson": poisson_model, "elo": elo, "n_matches": len(results),
            "results": results,
        }
    return models


def _leave_one_out_model(results: List[MatchResult], home: str, away: str, date: Optional[str]) -> Optional[PoissonGoalModel]:
    """Bitmiş bir maçı tahmin ederken kendi sonucunu eğitim setinden çıkarıp
    ayrı bir model döndürür. Eşleşme bulunamazsa veya çıkarma sonrası veri
    yetersiz kalırsa None döner (çağıran taraf bu durumda paylaşılan/ortak
    modele geri döner -- davranış öncekiyle aynı kalır)."""
    idx = None
    for i, r in enumerate(results):
        if r.home == home and r.away == away and r.date == date:
            idx = i
            break
    if idx is None:
        return None
    trimmed = results[:idx] + results[idx + 1:]
    if len(trimmed) < config.MIN_MATCHES_FOR_MODEL:
        return None
    m = PoissonGoalModel()
    m.fit(trimmed)
    return m


def _leave_one_out_elo(results: List[MatchResult], home: str, away: str, date: Optional[str]) -> Optional[EloRatings]:
    """_leave_one_out_model ile aynı mantık, Elo için: bitmiş bir maçın
    tahmininde kullanılacak Elo puanlarını, o maçın kendisi hiç oynanmamış
    gibi (sıradaki diğer tüm maçlarla, kronolojik sırayla) yeniden hesaplar.
    Elo sıralı/path-dependent olduğu için Poisson'daki gibi sadece o maçı
    çıkarıp kalanları eğitmek yeterli -- sıralama zaten korunuyor (results
    kronolojik). Eşleşme yoksa veya veri yetersizse None döner (çağıran
    taraf paylaşılan/ortak Elo'ya geri döner)."""
    idx = None
    for i, r in enumerate(results):
        if r.home == home and r.away == away and r.date == date:
            idx = i
            break
    if idx is None:
        return None
    trimmed = results[:idx] + results[idx + 1:]
    if len(trimmed) < config.MIN_MATCHES_FOR_MODEL:
        return None
    elo = EloRatings()
    for r in trimmed:
        elo.update_match(r.home, r.away, r.home_goals, r.away_goals)
    return elo


def score_str(score_block: Optional[dict]) -> str:
    if not score_block:
        return "-"
    h, a = score_block.get("home"), score_block.get("away")
    if h is None or a is None:
        return "-"
    return f"{h}-{a}"


def score_parts(score_block: Optional[dict]):
    """(ev_gol, deplasman_gol) -- sayı yoksa '-' döner, böylece ayrı sütunlarda gösterilebilir."""
    if not score_block:
        return "-", "-"
    h, a = score_block.get("home"), score_block.get("away")
    return (h if h is not None else "-", a if a is not None else "-")


def enrich_upcoming_matches(matches: List[dict]) -> Dict[str, dict]:
    """Yaklaşan maçlar için hakem / stadyum-şehir / hava durumu / kadro bilgisi
    toplar. RapidAPI ücretsiz planı ayda sadece 500 istek verdiği için SADECE
    config.ENRICHMENT_WINDOW_HOURS içinde başlayacak, en fazla
    config.ENRICHMENT_MAX_MATCHES kadar maça detay çağrısı yapılır."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(hours=config.ENRICHMENT_WINDOW_HOURS)
    lineup_window = dt.timedelta(hours=config.LINEUP_WINDOW_HOURS)
    grace = dt.timedelta(hours=2)  # maç başladıktan sonra da bir süre kadro/hakem çekmeye devam et

    candidates = []
    for m in matches:
        if m["status"] == "FINISHED":
            continue
        kickoff = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        delta = kickoff - now_utc
        if -grace <= delta <= window:
            candidates.append((delta, m))
    candidates.sort(key=lambda x: x[0])
    candidates = candidates[: config.ENRICHMENT_MAX_MATCHES]

    try:
        fs = FlashScoreClient()
    except Exception as e:
        print(f"  UYARI [FlashScore zenginleştirme]: {e}")
        return {}

    day_cache: Dict[str, list] = {}
    enrich: Dict[str, dict] = {}

    for delta, m in candidates:
        fs_id = m.get("flashscore_match_id")
        if not fs_id:
            date_part = m["utcDate"][:10]
            if date_part not in day_cache:
                try:
                    day_cache[date_part] = fs.get_matches_by_date(date_part)
                except FlashScoreError as e:
                    print(f"  UYARI [FlashScore gün verisi {date_part}]: {e}")
                    day_cache[date_part] = []
            fs_id = find_match_id_by_teams(day_cache[date_part], m["homeTeam"]["name"], m["awayTeam"]["name"])
        if not fs_id:
            continue

        details = fs.get_match_details(fs_id)
        hakem = "-"
        stad = "-"
        hava = "-"
        if details:
            hakem = details.get("referee") or "-"
            venue = details.get("venue") or {}
            vname, vcity = venue.get("name"), venue.get("city")
            if vname and vcity:
                stad = f"{vname} ({vcity})"
            elif vcity:
                stad = vcity
            if vcity:
                hava = get_weather_at(vcity, m["utcDate"]) or "-"

        kadro = "-"
        ev_key_missing = dep_key_missing = False
        ev_missing_name = dep_missing_name = None
        if delta <= lineup_window:
            lineups = fs.get_match_lineups(fs_id)
            if lineups and isinstance(lineups, list):
                by_side = {s.get("side"): s for s in lineups}
                home_side = by_side.get("home", {})
                away_side = by_side.get("away", {})
                ev_s = summarize_lineup_side(home_side) if home_side else "-"
                dep_s = summarize_lineup_side(away_side) if away_side else "-"
                kadro = f"EV: {ev_s}\nDEP: {dep_s}"

                # kilit oyuncu (ligin ilk 2 golcüsünden biri) kadroda yoksa
                # tahmine hafif bir hücum gücü düşüşü uygulanacak -- bkz. build_rows
                top_scorers = fs.get_top_scorers(m["competition"]["code"])
                if top_scorers and home_side:
                    for name in key_scorer_names(top_scorers, m["homeTeam"]["name"], config.KEY_PLAYER_TOP_N):
                        if player_missing_from_lineup(home_side, name):
                            ev_key_missing, ev_missing_name = True, name
                            break
                if top_scorers and away_side:
                    for name in key_scorer_names(top_scorers, m["awayTeam"]["name"], config.KEY_PLAYER_TOP_N):
                        if player_missing_from_lineup(away_side, name):
                            dep_key_missing, dep_missing_name = True, name
                            break

        enrich[m["id"]] = {
            "hakem": hakem, "stad": stad, "hava": hava, "kadro": kadro,
            "ev_key_missing": ev_key_missing, "dep_key_missing": dep_key_missing,
            "ev_missing_name": ev_missing_name, "dep_missing_name": dep_missing_name,
        }

    return enrich


def build_rows(matches: List[dict], models: Dict[str, dict], enrich: Optional[Dict[str, dict]] = None) -> List[list]:
    enrich = enrich or {}
    rows = []
    seen_ids = set()
    for m in sorted(matches, key=lambda x: x["utcDate"]):
        if m["id"] in seen_ids:
            continue
        seen_ids.add(m["id"])

        utc_dt = dt.datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone(ISTANBUL)
        gun = GUN_ADLARI[local_dt.weekday()]
        durum = DURUM_MAP.get(m["status"], m["status"])

        code = m["competition"]["code"]
        ulke = ULKE_MAP.get(code, "-")
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        iy_ev, iy_dep = score_parts(m["score"].get("halfTime"))
        ms_ev, ms_dep = score_parts(m["score"].get("fullTime"))

        ek = enrich.get(m["id"], {})
        hakem = ek.get("hakem", "-")
        stad = ek.get("stad", "-")
        hava = ek.get("hava", "-")
        kadro = ek.get("kadro", "-")

        model_info = models.get(code)
        tahmin_1x2 = ev_pct = draw_pct = away_pct = bek_ev = bek_dep = ust25 = alt25 = kg_var = kg_yok = ust15_pct = not_text = None
        cs_1x = cs_x2 = cs_12 = iy_ev_pct = iy_ber_pct = iy_dep_pct = en_olasi_skor = None

        if model_info is None:
            not_text = "Yetersiz geçmiş veri"
        else:
            pm: PoissonGoalModel = model_info["poisson"]
            elo_model: EloRatings = model_info["elo"]
            if durum == "Bitti":
                # kendi-kendini-doğrulama sızıntısını önle: bu maçın sonucu
                # olmadan eğitilmiş bir model/Elo kullan (bkz. _leave_one_out_model,
                # _leave_one_out_elo) -- Elo için de aynı düzeltme gerekli, yoksa
                # Poisson tarafında kapattığımız sızıntı Elo üzerinden geri girer.
                loo = _leave_one_out_model(model_info["results"], home, away, m["utcDate"])
                if loo is not None:
                    pm = loo
                loo_elo = _leave_one_out_elo(model_info["results"], home, away, m["utcDate"])
                if loo_elo is not None:
                    elo_model = loo_elo
            home_mult = config.KEY_PLAYER_MISSING_ATTACK_MULT if ek.get("ev_key_missing") else 1.0
            away_mult = config.KEY_PLAYER_MISSING_ATTACK_MULT if ek.get("dep_key_missing") else 1.0
            pred = pm.predict(
                home, away, home_attack_mult=home_mult, away_attack_mult=away_mult,
                elo=elo_model, elo_weight=config.ELO_BLEND_WEIGHT,
            )
            probs = pred["1x2"]
            best = max(probs, key=probs.get)
            tahmin_1x2 = {"ev_kazanir": "1", "beraberlik": "X", "deplasman_kazanir": "2"}[best]
            ev_pct = round(probs["ev_kazanir"] * 100, 1)
            draw_pct = round(probs["beraberlik"] * 100, 1)
            away_pct = round(probs["deplasman_kazanir"] * 100, 1)
            bek_ev = pred["beklenen_gol_ev"]
            bek_dep = pred["beklenen_gol_deplasman"]
            ust25 = round(pred["2.5_gol"]["ust"] * 100, 1)
            alt25 = round(pred["2.5_gol"]["alt"] * 100, 1)
            kg_var = round(pred["kg"]["var"] * 100, 1)
            kg_yok = round(pred["kg"]["yok"] * 100, 1)
            ust15_pct = round(pred["1.5_gol"]["ust"] * 100, 1)

            # yeni: çifte şans, ilk yarı tahmini, en olası skor (mevcut sütunları
            # bozmadan MODEL TAHMİNİ bloğunun sonuna ekleniyor)
            cs = pred["cifte_sans"]
            cs_1x = round(cs["1x"] * 100, 1)
            cs_x2 = round(cs["x2"] * 100, 1)
            cs_12 = round(cs["12"] * 100, 1)
            iy = pred["ilk_yari"]
            iy_ev_pct = round(iy["ev"] * 100, 1)
            iy_ber_pct = round(iy["beraberlik"] * 100, 1)
            iy_dep_pct = round(iy["deplasman"] * 100, 1)
            top_skor = pred["en_olasi_skorlar"][0]
            en_olasi_skor = f"{top_skor['skor']} (%{round(top_skor['olasilik'] * 100)})"

            # şeffaflık: tahmin kadro yüzünden ayarlandıysa "Not" sütununda açıkça yazsın
            missing_notes = []
            if ek.get("ev_key_missing"):
                missing_notes.append(f"Ev kilit oyuncu eksik ({ek.get('ev_missing_name')}) -> hücum -%15")
            if ek.get("dep_key_missing"):
                missing_notes.append(f"Dep kilit oyuncu eksik ({ek.get('dep_missing_name')}) -> hücum -%15")
            if missing_notes:
                not_text = " | ".join(missing_notes)

        rows.append([
            local_dt.strftime("%d.%m.%Y"),
            gun,
            local_dt.strftime("%H:%M"),
            ulke,
            m["competition"]["name"],
            home,
            away,
            durum,
            iy_ev,
            iy_dep,
            ms_ev,
            ms_dep,
            tahmin_1x2,
            ev_pct,
            draw_pct,
            away_pct,
            bek_ev,
            bek_dep,
            ust25,
            alt25,
            kg_var,
            kg_yok,
            ust15_pct,
            cs_1x,
            cs_x2,
            cs_12,
            iy_ev_pct,
            iy_ber_pct,
            iy_dep_pct,
            en_olasi_skor,
            not_text,
            hakem,
            stad,
            hava,
            kadro,
        ])
    return rows


HIGHLIGHT_HEADERS = ["Tarih", "Saat", "Ülke", "Lig", "Ev Sahibi", "Deplasman",
                      "Pazar", "Seçim", "Olasılık %"]


def build_highlights_rows(rows: List[list], top_n: Optional[int] = None) -> List[list]:
    """'Öne Çıkanlar': her GÜN için (henüz oynanmamış maçlar arasından) 1.5
    Üst % en yüksekten en düşüğe sıralı TÜM maçlar + Çifte Şans %
    (1X/X2/12'nin en iyisi) en yüksekten en düşüğe sıralı TÜM maçlar.
    top_n=None (varsayılan) o gün kaç uygun maç varsa hepsini listeler --
    sabit bir sayı yok, dinamik. top_n bir sayı verilirse sadece o kadarını
    keser (geriye dönük uyumluluk / istenirse sınırlama için). `rows` zaten
    build_rows()'tan kronolojik geliyor (bkz. build_rows: sorted by
    utcDate), bu yüzden gün grupları da sırayla çıkar.

    Girdi satır indeksleri (0-indeksli, build_rows ile aynı):
      0 Tarih, 2 Saat, 3 Ülke, 4 Lig, 5 Ev Sahibi, 6 Deplasman, 7 Durum,
      22 1.5 Üst %, 23 1X %, 24 X2 %, 25 12 %
    Çıktı sütunları: HIGHLIGHT_HEADERS ile aynı sırada.
    """
    by_date: "OrderedDict[str, list]" = OrderedDict()
    for row in rows:
        if row[7] == "Bitti":
            continue
        by_date.setdefault(row[0], []).append(row)

    out: List[list] = []
    cs_labels = ["1X", "X2", "12"]

    for tarih, day_rows in by_date.items():
        # --- 1.5 Üst en iyileri ---
        ust15_cands = [r for r in day_rows if r[22] is not None]
        ust15_cands.sort(key=lambda r: r[22], reverse=True)
        for r in ust15_cands[:top_n]:
            out.append([r[0], r[2], r[3], r[4], r[5], r[6], "1.5 Üst", "Üst", round(r[22], 1)])

        # --- Çifte Şans en iyileri (1X/X2/12 arasından en yüksek olan) ---
        cs_cands = []
        for r in day_rows:
            opts = [r[23], r[24], r[25]]
            if any(o is None for o in opts):
                continue
            best_i = max(range(3), key=lambda i: opts[i])
            cs_cands.append((r, cs_labels[best_i], opts[best_i]))
        cs_cands.sort(key=lambda t: t[2], reverse=True)
        for r, label, val in cs_cands[:top_n]:
            out.append([r[0], r[2], r[3], r[4], r[5], r[6], "Çifte Şans", label, round(val, 1)])

    return out


def write_highlights_sheet(wb, highlight_rows: List[list]) -> None:
    """'Öne Çıkanlar' adında yeni bir sekme ekler: ana tablodaki stil
    mantığının sadeleştirilmiş hali (tek satır başlık + gün bazlı zebra +
    pazar rengine göre vurgulu 'Olasılık %' hücresi)."""
    ws = wb.create_sheet("Öne Çıkanlar")

    FONT_NAME = "Calibri"
    white_bold = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=11)
    base_font = Font(name=FONT_NAME, size=10)
    team_font = Font(name=FONT_NAME, size=10, bold=True, color="1A1A1A")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    header_fill = PatternFill(start_color="27AE60", end_color="27AE60", fill_type="solid")
    ust15_fill = PatternFill(start_color="EAF7EF", end_color="EAF7EF", fill_type="solid")
    cs_fill = PatternFill(start_color="E8F6F3", end_color="E8F6F3", fill_type="solid")
    date_band_fill = PatternFill(start_color="2C3E50", end_color="2C3E50", fill_type="solid")

    n_cols = len(HIGHLIGHT_HEADERS)
    for c, title in enumerate(HIGHLIGHT_HEADERS, start=1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.font = white_bold
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[1].height = 22

    r = 2
    last_tarih = None
    for row in highlight_rows:
        tarih = row[0]
        if tarih != last_tarih:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_cols)
            band_cell = ws.cell(row=r, column=1, value=f"  {tarih}")
            band_cell.font = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
            band_cell.fill = date_band_fill
            band_cell.alignment = left
            for c in range(1, n_cols + 1):
                ws.cell(row=r, column=c).fill = date_band_fill
                ws.cell(row=r, column=c).border = border
            last_tarih = tarih
            r += 1

        pazar = row[6]
        fill = ust15_fill if pazar == "1.5 Üst" else cs_fill
        for c, val in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = base_font
            cell.border = border
            cell.alignment = center
            cell.fill = fill
        ws.cell(row=r, column=5).font = team_font
        ws.cell(row=r, column=5).alignment = left
        ws.cell(row=r, column=6).font = team_font
        ws.cell(row=r, column=6).alignment = left
        oran_cell = ws.cell(row=r, column=9)
        oran_cell.number_format = "0.0"
        oran_cell.font = Font(name=FONT_NAME, size=10, bold=True,
                               color="1E8449" if pazar == "1.5 Üst" else "117864")
        r += 1

    widths = [11, 6, 10, 19, 20, 20, 11, 9, 11]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    if r > 2:
        ws.auto_filter.ref = f"A1:{get_column_letter(n_cols)}{r - 1}"


# Sütun sırası (1-indeksli, aşağıdaki grup/stil haritalarıyla hizalı):
#  1 Tarih  2 Gün  3 Saat  4 Ülke  5 Lig  6 Ev Sahibi  7 Deplasman  8 Durum
#  9 İY-Ev  10 İY-Dep  11 MS-Ev  12 MS-Dep
#  13 Tahmin  14 Ev%  15 Ber.%  16 Dep.%  17 Bek.Ev  18 Bek.Dep  19 2.5Üst%  20 2.5Alt%
#  21 KGVar%  22 KGYok%  23 1.5Üst%
#  24 1X%  25 X2%  26 12%  (çifte şans)
#  27 İY-Ev%  28 İY-Ber%  29 İY-Dep%  (ilk yarı tahmini)
#  30 En Olası Skor
#  31 Not
#  32 Hakem  33 Stad/Şehir  34 Hava  35 Kadro
SUB_HEADERS = [
    "Tarih", "Gün", "Saat", "Ülke", "Lig", "Ev Sahibi", "Deplasman", "Durum",
    "Ev", "Dep", "Ev", "Dep",
    "1/X/2", "Ev %", "Ber %", "Dep %", "Ev", "Dep",
    "2.5 Üst %", "2.5 Alt %", "Var %", "Yok %", "1.5 Üst %",
    "1X %", "X2 %", "12 %",
    "Ev %", "Ber %", "Dep %",
    "En Olası Skor",
    "Not",
    "Hakem", "Stad / Şehir", "Hava", "Kadro",
]
N_COLS = len(SUB_HEADERS)

# (başlık, kaç sütun kaplıyor, dolgu rengi) -- row1'de birleştirilecek grup bantları
GROUPS = [
    ("MAÇ BİLGİSİ", 3, "34495E"),
    ("LİG", 2, "16A085"),
    ("TAKIMLAR", 2, "2C3E50"),
    ("DURUM", 1, "E67E22"),
    ("İY SKOR", 2, "3498DB"),
    ("MS SKOR", 2, "1F618D"),
    ("MODEL TAHMİNİ", 11, "27AE60"),
    ("ÇİFTE ŞANS", 3, "1ABC9C"),
    ("İLK YARI TAHMİNİ", 3, "2980B9"),
    ("EN OLASI SKOR", 1, "138D75"),
    ("NOT", 1, "7F8C8D"),
    ("EK BİLGİ (Hakem/Hava/Kadro)", 4, "8E44AD"),
]

TAHMIN_RENK = {"1": "1E8449", "X": "707B7C", "2": "922B21"}  # ev yeşil / berabere gri / deplasman kırmızı


def write_excel(rows: List[list], out_path: str, meta: dict) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Maçlar ve Tahminler"

    FONT_NAME = "Calibri"
    white_bold = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
    sub_font = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=9)
    base_font = Font(name=FONT_NAME, size=10)
    team_font = Font(name=FONT_NAME, size=10, bold=True, color="1A1A1A")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center")
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    finished_fill = PatternFill(start_color="EDEDED", end_color="EDEDED", fill_type="solid")
    scheduled_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
    live_fill = PatternFill(start_color="FDEBD0", end_color="FDEBD0", fill_type="solid")
    pred_block_fill = PatternFill(start_color="EAF7EF", end_color="EAF7EF", fill_type="solid")
    zebra_fill = PatternFill(start_color="F7F9FA", end_color="F7F9FA", fill_type="solid")

    # --- Satır 1: grup bantları (birleştirilmiş) ---
    col = 1
    for title, span, color in GROUPS:
        fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        if span > 1:
            ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + span - 1)
        else:
            ws.merge_cells(start_row=1, start_column=col, end_row=2, end_column=col)
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = white_bold
        cell.fill = fill
        cell.alignment = center
        cell.border = border
        for c in range(col, col + span):
            ws.cell(row=1, column=c).fill = fill
            ws.cell(row=1, column=c).border = border
            if span == 1:
                ws.cell(row=2, column=c).fill = fill
        col += span

    # --- Satır 2: alt başlıklar (gruplar içindeki tek tek sütunlar) ---
    col = 1
    for title, span, color in GROUPS:
        # alt başlık rengi: grup renginin biraz koyusu/aynısı, biraz saydam görünüm için sabit koyu ton kullan
        sub_fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
        for i in range(span):
            c = col + i
            if span > 1:
                cell = ws.cell(row=2, column=c, value=SUB_HEADERS[c - 1])
                cell.font = sub_font
                cell.fill = sub_fill
                cell.alignment = center
                cell.border = border
        col += span

    for col_idx in range(1, N_COLS + 1):
        ws.cell(row=2, column=col_idx).border = border

    # --- Veri satırları ---
    for row in rows:
        ws.append(row)

    last_row = ws.max_row
    DURUM_COL = 8
    TAHMIN_COL = 13
    for r in range(3, last_row + 1):
        durum_val = ws.cell(row=r, column=DURUM_COL).value
        is_finished = durum_val == "Bitti"
        is_live = durum_val in ("Canlı", "Devre Arası")
        zebra = (r % 2 == 0)

        for c in range(1, N_COLS + 1):
            cell = ws.cell(row=r, column=c)
            cell.font = base_font
            cell.border = border
            cell.alignment = center

            # satır arkaplanı: önce durum, yoksa zebra
            if is_live:
                cell.fill = live_fill
            elif is_finished:
                cell.fill = finished_fill
            elif zebra:
                cell.fill = zebra_fill
            else:
                cell.fill = scheduled_fill

            # model tahmini bloğu (13-30: 1/X/2...1.5Üst%, çifte şans, ilk yarı,
            # en olası skor) hafif yeşil arka planla öne çıksın
            # (durum canlı/bitti rengi zaten üstte ezdiği için sadece scheduled/zebra'da uygula)
            if 13 <= c <= 30 and not is_live and not is_finished:
                cell.fill = pred_block_fill

        # takım isimleri kalın
        ws.cell(row=r, column=6).font = team_font
        ws.cell(row=r, column=6).alignment = left
        ws.cell(row=r, column=7).font = team_font
        ws.cell(row=r, column=7).alignment = left

        # yüzde sütunları: ondalık format
        for c in (14, 15, 16, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29):
            cell = ws.cell(row=r, column=c)
            if cell.value is not None:
                cell.number_format = "0.0"
        for c in (17, 18):
            cell = ws.cell(row=r, column=c)
            if cell.value is not None:
                cell.number_format = "0.00"

        # tahmin hücresi: 1/X/2'ye göre renkli kalın yazı
        tahmin_cell = ws.cell(row=r, column=TAHMIN_COL)
        tv = tahmin_cell.value
        if tv in TAHMIN_RENK:
            tahmin_cell.font = Font(name=FONT_NAME, size=11, bold=True, color=TAHMIN_RENK[tv])

    widths = [11, 11, 6, 10, 19, 20, 20, 12, 5, 5, 5, 5, 7, 7, 7, 7, 6, 6, 7, 7, 7, 7, 7,
              7, 7, 7, 7, 7, 7, 14, 18,
              14, 24, 20, 45]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.row_dimensions[1].height = 20
    ws.row_dimensions[2].height = 24

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(N_COLS)}{last_row}"

    # --- Öne Çıkanlar sekmesi (en iyi 1.5 Üst + Çifte Şans, gün gün) ---
    highlight_rows = build_highlights_rows(rows)
    write_highlights_sheet(wb, highlight_rows)

    # --- Açıklama / lejant sekmesi ---
    info = wb.create_sheet("Bilgi")
    info["A1"] = "Futbol Maç Tahminleme Sistemi - Rapor Notları"
    info["A1"].font = Font(name=FONT_NAME, bold=True, size=12)
    notes = [
        "",
        f"Oluşturulma zamanı: {meta['generated_at']}",
        f"Tarih aralığı: {meta['start']} -> {meta['end']}",
        f"Kapsanan turnuvalar: {', '.join(meta['competitions'])}",
        "",
        "RENK KODLARI",
        "- Gri satır: maç bitti (gerçek skor kesinleşti)",
        "- Turuncu satır: maç şu an canlı / devre arasında",
        "- Beyaz / açık gri (zebra): maç henüz oynanmadı",
        "- 'Model Tahmini' bloğu (açık yeşil): henüz oynanmamış maçlarda tahmin öne çıkarılır",
        "- Tahmin hücresi: 1 = yeşil (ev kazanır), X = gri (beraberlik), 2 = kırmızı (deplasman kazanır)",
        "",
        "Veri kaynağı: football-data.org (ücretsiz plan) -- Premier League,",
        "La Liga, Serie A, Bundesliga, Ligue 1, Şampiyonlar Ligi, Eredivisie,",
        "Primeira Liga, Brezilya Serie A. Bunların dışındakiler (Süper Lig,",
        "TFF 1. Lig, Jupiler Pro League, Arjantin Liga Profesional, UEFA Avrupa",
        "Ligi, UEFA Avrupa Konferans Ligi, UEFA Uluslar Ligi) FlashScore (RapidAPI)",
        "üzerinden",
        "ayrıca çekilir ve aynı modelle (Poisson + Elo) işlenir -- amatör/kadın/",
        "gençlik ligleri ve dünyadaki diğer tüm ligler KAPSAM DIŞI (hem veri",
        "kalitesi hem RapidAPI kotası nedeniyle).",
        "",
        "Model: Her turnuva için ayrı Poisson gol modeli + Elo derecelendirmesi,",
        "o turnuvaya ait tarih aralığındaki BİTMİŞ maçlardan eğitilir.",
        f"Bir turnuvada en az {config.MIN_MATCHES_FOR_MODEL} bitmiş maç yoksa tahmin",
        "üretilmez ('Not' sütununda 'Yetersiz geçmiş veri' olarak işaretlenir).",
        "",
        "EK BİLGİ (Hakem / Stad / Hava / Kadro): FlashScore'dan, SADECE maça",
        f"{config.ENRICHMENT_WINDOW_HOURS} saatten az kalan maçlar için çekilir (API kotası",
        "kısıtlı olduğundan). Kadro sütununda '(Tahmini)' etiketi FlashScore'un",
        "son maçlara göre algoritmik tahminini, '(Kesin)' etiketi kulüpçe",
        "açıklanmış resmi kadroyu gösterir -- resmi kadro genelde maçtan",
        "60-75 dakika önce yayınlanır.",
        "",
        "Bu tablo istatistiksel bir analiz/eğlence aracıdır; kesin sonuç",
        "garantisi vermez, bahis tavsiyesi değildir.",
    ]
    for i, line in enumerate(notes, start=2):
        info.cell(row=i, column=1, value=line).font = base_font
    info.column_dimensions["A"].width = 85

    wb.save(out_path)


def main():
    parser = argparse.ArgumentParser(description="Avrupa ligleri maç + tahmin tablosunu Excel'e aktar")
    parser.add_argument("--lookback-days", type=int, default=40,
                         help="Model eğitimi için geriye kaç gün taransın (ne kadar çok, o kadar sağlam istatistik)")
    parser.add_argument("--display-lookback-days", type=int, default=3,
                         help="Tabloda GÖSTERİLECEK geçmiş gün sayısı (eğitim penceresinden ayrı, daha küçük tutulur)")
    parser.add_argument("--forward-days", type=int, default=3)
    parser.add_argument("--competitions", nargs="*", default=list(config.COMPETITIONS.keys()))
    parser.add_argument("--no-super-lig", action="store_true",
                         help="Süper Lig'i (FlashScore kaynaklı) pipeline'a dahil etme")
    parser.add_argument("--no-extra-leagues", action="store_true",
                         help="TFF 1. Lig / Jupiler Pro League / Arjantin / Avrupa Ligi / Konferans Ligi'ni (FlashScore kaynaklı) pipeline'a dahil etme")
    parser.add_argument("--no-enrichment", action="store_true",
                         help="Hakem/stad/hava/kadro zenginleştirmesini atla (FlashScore istek kotasını korur)")
    parser.add_argument("--out", default="maclar_ve_tahminler.xlsx")
    args = parser.parse_args()

    today = dt.datetime.now(tz=ISTANBUL).date()
    train_start = today - dt.timedelta(days=args.lookback_days)
    display_start = today - dt.timedelta(days=args.display_lookback_days)

    # Milli ara gibi dönemlerde önümüzdeki --forward-days içinde hiç maç
    # olmayabilir. Bu yüzden veri çekme ucu her zaman biraz daha ileriye
    # (SEARCH_FORWARD_DAYS) açılır; gösterim penceresi ise "en yakın maçın
    # olduğu günden başlayıp forward_days kadar süren" bir aralık olarak
    # ayrıca hesaplanır -- yani Serra her çalıştırmada BOŞ bir tablo değil,
    # en yakın N günlük gerçek maç bloğunu görür.
    SEARCH_FORWARD_DAYS = max(args.forward_days, 30)
    fetch_end = today + dt.timedelta(days=SEARCH_FORWARD_DAYS)

    comp_codes = [config.COMPETITIONS[k] for k in args.competitions]

    client = FootballDataClient()
    print(f"Eğitim için taranıyor: {train_start} -> {fetch_end}  Turnuvalar: {args.competitions}")
    matches = collect_matches(client, comp_codes, train_start, fetch_end)
    print(f"Toplam maç bulundu (eğitim havuzu, football-data.org): {len(matches)}")

    comp_labels = list(args.competitions)
    if not args.no_super_lig:
        sl_matches = collect_super_lig_matches()
        print(f"Süper Lig (FlashScore): {len(sl_matches)} maç")
        matches.extend(sl_matches)
        comp_labels.append("super_lig")

    if not args.no_extra_leagues:
        # NOT: kota durumuna bakıp bir ligi hiç DENEMEDEN atlamıyoruz --
        # cache (7 gün TTL) zaten çoğu zaman hiç ağ isteği gerektirmiyor, bu
        # yüzden "kalan istek az" diye önceden atlamak, bedavaya gelecek
        # cache'lenmiş veriyi de boşuna feda ederdi. Her lig kendi
        # cache-önce / 429'da bayat-cache'e-düş / son çare kalıcı tarihçe
        # zincirini (bkz. flashscore_client.get, _merge_with_history) zaten
        # kendi içinde yürütüyor; burada sadece deniyoruz.
        for league_key, league_info in config.FLASHSCORE_EXTRA_LEAGUES.items():
            extra_matches = collect_flashscore_extra_league(league_key)
            print(f"{league_info['name']} (FlashScore): {len(extra_matches)} maç")
            matches.extend(extra_matches)
            comp_labels.append(league_info["name"])

    # football-data.org kaynaklı maçlar (Premier League, La Liga, Serie A,
    # Bundesliga, Ligue 1, Eredivisie, Primeira Liga, Brezilya Serie A,
    # Championship, UCL) 'canlı boşluk' düzeltmesinden FlashScore maçları
    # gibi geçmiyordu -- bu yüzden maç başladıktan sonra football-data.org
    # cache'i (TTL 6 saat) dolana kadar tabloda 'Planlandı' kalabiliyordu.
    # Aynı (zaten ucuz/paylaşılan) matches/list-by-date taramasını kullanıp
    # takım adıyla eşleştirerek bunları da yerinde düzeltiyoruz.
    patch_football_data_live_gap(matches)

    models = build_models_per_competition(matches)
    for code, m in models.items():
        name = next((k for k, v in config.COMPETITIONS.items() if v == code), code)
        if m is None:
            print(f"  [{name}] yetersiz veri, tahmin yok")
        else:
            print(f"  [{name}] {m['n_matches']} bitmiş maçla model eğitildi")

    # En yakın (bugünden itibaren) bitmemiş maçın tarihini bul; gösterim
    # penceresinin bitişi o günden başlayıp forward_days kadar sürer.
    upcoming_dates = sorted({
        m["utcDate"][:10] for m in matches
        if m["status"] != "FINISHED" and m["utcDate"][:10] >= today.isoformat()
    })
    if upcoming_dates:
        window_start_date = dt.date.fromisoformat(upcoming_dates[0])
    else:
        window_start_date = today
    end = window_start_date + dt.timedelta(days=args.forward_days - 1)

    # Tabloda sadece display_start -> end aralığındaki maçlar gösterilir;
    # modelin eğitildiği daha geniş geçmiş havuzu sadece istatistik içindir.
    display_matches = [
        m for m in matches
        if display_start.isoformat() <= m["utcDate"][:10] <= end.isoformat()
    ]

    enrich = {}
    if not args.no_enrichment:
        enrich = enrich_upcoming_matches(display_matches)
        print(f"FlashScore zenginleştirme: {len(enrich)} maç için hakem/stad/hava/kadro bulundu")

    rows = build_rows(display_matches, models, enrich)

    meta = {
        "generated_at": dt.datetime.now(tz=ISTANBUL).strftime("%d.%m.%Y %H:%M"),
        "start": display_start.isoformat(),
        "end": end.isoformat(),
        "competitions": comp_labels,
    }
    write_excel(rows, args.out, meta)
    highlight_rows = build_highlights_rows(rows)
    print(f"Yazıldı: {args.out} ({len(rows)} satır, gösterim aralığı {display_start} -> {end})")
    print(f"Öne Çıkanlar: {len(highlight_rows)} satır (1.5 Üst + Çifte Şans, gün gün en iyiler)")

    quota = FlashScoreClient().get_quota_status()
    if quota and quota.get("remaining") is not None:
        limit_txt = f"/{quota['limit']}" if quota.get("limit") is not None else ""
        print(f"RapidAPI (FlashScore) kota durumu: kalan {quota['remaining']}{limit_txt} istek (son görülen: {quota.get('checked_at', '-')})")


if __name__ == "__main__":
    main()
