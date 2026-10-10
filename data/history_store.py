"""
Kalıcı maç tarihçesi deposu (FlashScore ek ligleri + Süper Lig için).

NEDEN: 'tournaments/results' RapidAPI çağrısı tüm sezonu baştan döndürüyor
ve pahalı (aylık 500 isteklik kotanın büyük kısmını yiyor) -- bu yüzden
flashscore_client.py'de artık uzun TTL (7 gün) ile cache'leniyor. Ama bu,
kota tamamen tükendiğinde veya TTL dolmadan önce o çağrı bir süre hiç
başarılı olmazsa, yeni biten maçların training havuzuna GİRMEMESİ riskini
taşıyor -- 'canlı boşluk' düzeltmesi (export_excel.collect_live_gap_matches)
sadece son LIVE_GAP_LOOKBACK_DAYS günü kapsıyor, daha eskisini değil.

ÇÖZÜM: her lig için bitmiş maçları diske SÜRESİZ (asla silinmeyen,
asla TTL'i dolmayan) bir JSON dosyasında biriktiriyoruz. Her çalıştırmada:
  1) o lig için ne kadar 'results'/'fixtures'/'canlı boşluk' verisi elde
     edildiyse, içindeki BİTMİŞ maçlar bu depoya eklenir (merge_into_history),
  2) depodaki TÜM bilinen bitmiş maçlar, training havuzuna geri verilir.
Böylece 'tournaments/results' çağrısı aylarca başarısız olsa bile, ucuz
'matches/list-by-date' (canlı boşluk) çağrısı çalıştığı sürece depo günbegün
büyümeye devam eder ve training havuzu eksilmez -- sadece büyür.
"""
import json
import os
from typing import Dict, List

import config

# NOT: cache/ klasörünün içinde DEĞİL -- cache/ tamamen tekrar-üretilebilir
# (.gitignore'da, TTL'li, kaybolsa da sorun olmaz) ama bu tarihçe TERS:
# SÜRESİZ ve kasıtlı olarak büyüyen, kaybedilmemesi gereken veri. Bu yüzden
# repo köküne ayrı bir klasöre yazılır ve git'e commit edilir -- böylece bu
# bulut oturumu/konteyner sıfırlansa bile (cache/ gibi) kaybolmaz, bir
# sonraki clone'da/oturumda aynen devam eder.
HISTORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "match_history")


def _history_path(competition_code: str) -> str:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    safe = competition_code.replace("/", "_")
    return os.path.join(HISTORY_DIR, f"{safe}.json")


def load_history(competition_code: str) -> Dict[str, dict]:
    """match_id -> maç dict (football-data.org benzeri şekil). Dosya yoksa boş dict."""
    path = _history_path(competition_code)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # bozuk/yarım yazılmış dosya -- tarihçeyi kaybetmek yerine boştan başla
        return {}


def save_history(competition_code: str, by_id: Dict[str, dict]) -> None:
    path = _history_path(competition_code)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(by_id, f, ensure_ascii=False)
    os.replace(tmp_path, path)  # atomik -- yarım yazım riskini önler


def merge_into_history(competition_code: str, new_matches: List[dict]) -> List[dict]:
    """new_matches içindeki BİTMİŞ maçları depoya ekler (match_id ile dedupe,
    yeni gelen -- daha güncel skor/durum olabileceği için -- eskinin üstüne
    yazar) ve depodaki TÜM bitmiş maçların güncel listesini döndürür.

    'id' alanı (football-data.org / fs_match_to_fd_shape'teki) anahtar olarak
    kullanılır -- FlashScore maçları için bu "fs_<match_id>" şeklinde, yani
    ligler arası çakışma olmaz."""
    by_id = load_history(competition_code)
    changed = False
    for m in new_matches:
        if m.get("status") != "FINISHED":
            continue
        mid = m.get("id")
        if not mid:
            continue
        if by_id.get(mid) != m:
            by_id[mid] = m
            changed = True
    if changed:
        save_history(competition_code, by_id)
    return list(by_id.values())


def history_count(competition_code: str) -> int:
    return len(load_history(competition_code))
