"""
Proje ayarları.

Veri kaynağı: football-data.org (v4 API)
  - Ücretsiz plan: Şampiyonlar Ligi, Premier League, La Liga, Bundesliga,
    Ligue 1, Serie A, Eredivisie, Primeira Liga, Championship, Dünya Kupası,
    Avrupa Şampiyonası, Brezilya Serie A - SONSUZA DEK ücretsiz.
  - Süper Lig bu planda YOK (bkz. areas/mac-tahmin-sistemi notları).
  - Dakikalık istek limiti var (~10/dk); data/football_data_client.py
    tüm cevapları cache'ler.

API anahtarı (token) almak için:
  1) https://www.football-data.org/client/register üzerinden ücretsiz
     kayıt ol.
  2) Hesabından aldığın token'ı .env dosyasına ekle:
     FOOTBALL_DATA_TOKEN=xxxxxxxxxxxxxxxx
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---- football-data.org (aktif veri kaynağı) ----
FOOTBALL_DATA_TOKEN = os.getenv("FOOTBALL_DATA_TOKEN", "")
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"

# Takip edilecek turnuvalar (football-data.org competition kodları).
# Süper Lig free tier'da olmadığı için şimdilik listede değil.
COMPETITIONS = {
    "premier_league": "PL",   # İngiltere
    "la_liga": "PD",          # İspanya (Primera Division)
    "serie_a": "SA",          # İtalya
    "bundesliga": "BL1",      # Almanya
    "ligue_1": "FL1",         # Fransa
    "ucl": "CL",              # UEFA Şampiyonlar Ligi
    "eredivisie": "DED",      # Hollanda
    "primeira_liga": "PPL",   # Portekiz
    "brezilya_serie_a": "BSA",  # Brezilya
    "championship": "ELC",   # İngiltere 2. lig
}

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_TTL_HOURS = 6  # maç durumları (skor, saat) güncellenebildiği için kısa TTL

# ---- eski veri kaynağı: API-Football (kullanılmıyor; hesap askıya alındı) ----
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_HOST = os.getenv("API_FOOTBALL_HOST", "v3.football.api-sports.io")
API_FOOTBALL_BASE_URL = f"https://{API_FOOTBALL_HOST}"
LEAGUES = {
    "super_lig": 203,
    "premier_league": 39,
    "la_liga": 140,
    "serie_a": 135,
    "bundesliga": 78,
    "ligue_1": 61,
    "ucl": 2,
}
CURRENT_SEASON = int(os.getenv("SEASON", "2026"))

# ---- FlashScore (RapidAPI "flashscore4") -- Süper Lig + hakem/stad/kadro/hava zenginleştirme ----
# Ücretsiz RapidAPI planı AYDA SADECE 500 istek veriyor -- bu yüzden:
#   - Süper Lig fikstür/sonuç çağrıları cache'lenir (TTL ~6 saat).
#   - Hakem/stad/kadro zenginleştirmesi SADECE yakın zamanda (ENRICHMENT_WINDOW_HOURS
#     içinde) başlayacak, en fazla ENRICHMENT_MAX_MATCHES kadar maça uygulanır.
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

# Bitmiş maç sonuçları ("tournaments/results") bir kere çekildikten sonra asla
# değişmez -- aynı gün içinde art arda "Güncelle" çalıştırıldığında bu veriyi
# tekrar tekrar RapidAPI'den çekip aylık 500 isteklik kotayı eritmemek için
# uzun bir TTL kullanılır (7 gün). Fikstür (henüz oynanmamış maçlar) çağrıları
# bunun dışında -- onlar hâlâ kısa TTL (6 saat) ile tazeleniyor çünkü programı
# değişebilir / yeni maçlar eklenebilir.
FLASHSCORE_RESULTS_TTL_HOURS = 24.0 * 7

# "tournaments/results" bayatken bile, az önce biten bir maçın hâlâ
# 'Planlandı' görünüp kalmaması için matches/list-by-date ile kaç günlük
# bir pencerenin (bugün dahil, geriye doğru) canlı/bitti durumu kontrol
# edilir. display-lookback-days ile aynı mantık -- varsayılan 2 gün.
LIVE_GAP_LOOKBACK_DAYS = 2

SUPER_LIG = {
    "country_id": 191,
    "tournament_id": "ABdATjMP",
    "tournament_stage_id": "2TRNmxYR",
    "tournament_template_id": "Opdcd08Q",
    "season_id": "190",
    "code": "SUPER_LIG",
    "name": "Süper Lig",
}

# FlashScore tournament_id / tournament_stage_id eşlemesi -- top-scorer bazlı
# "kilit oyuncu eksik mi" kontrolü için (bkz. flashscore_client.get_top_scorers).
# football-data.org kodlarıyla (PL/PD/SA/BL1/FL1/CL) + SUPER_LIG eşleşiyor.
FLASHSCORE_TOURNAMENTS = {
    "PL": {"tournament_id": "SY30SsKF", "tournament_stage_id": "CfoA8Dmm"},
    "PD": {"tournament_id": "QeI1Oeyi", "tournament_stage_id": "dWdJXP6U"},
    "SA": {"tournament_id": "WdNk9YwP", "tournament_stage_id": "CfujcOgK"},
    "BL1": {"tournament_id": "KY7LrA6d", "tournament_stage_id": "jg0MwVuC"},
    "FL1": {"tournament_id": "0Mp0HlJb", "tournament_stage_id": "CQF28KxR"},
    "CL": {"tournament_id": "tfRdlhP9", "tournament_stage_id": "vT1iNRGq"},
    "SUPER_LIG": {"tournament_id": "ABdATjMP", "tournament_stage_id": "2TRNmxYR"},
}

# football-data.org'da OLMAYAN ek ligler -- Süper Lig ile aynı yöntemle
# (tournaments/results + tournaments/fixtures, tournament_template_id +
# season_id ile) FlashScore üzerinden çekilir. ID'ler general/tournaments
# (ülkeye göre lig listesi) + tournaments/ids (url -> id eşlemesi) ile
# tek seferlik bulundu, sezon boyunca değişmez.
FLASHSCORE_EXTRA_LEAGUES = {
    "TFF1": {
        "code": "TFF1", "name": "TFF 1. Lig", "ulke": "Türkiye",
        "tournament_id": "4j3BAWrp", "tournament_stage_id": "llS6O0mJ",
        "tournament_template_id": "Spw6Az1D", "season_id": "190",
    },
    "JPL": {
        "code": "JPL", "name": "Jupiler Pro League", "ulke": "Belçika",
        "tournament_id": "QNIOJe2e", "tournament_stage_id": "MFaWsPnS",
        "tournament_template_id": "dG2SqPrf", "season_id": "190",
    },
    "ARG": {
        "code": "ARG", "name": "Liga Profesional (Arjantin)", "ulke": "Arjantin",
        "tournament_id": "j1MnZ14E", "tournament_stage_id": "dEDMdu1s",
        "tournament_template_id": "naYhNOaA", "season_id": "185",
    },
    "UEL": {
        "code": "UEL", "name": "UEFA Avrupa Ligi", "ulke": "Avrupa",
        "tournament_id": "nTFvUo9N", "tournament_stage_id": "OjQOTQKa",
        "tournament_template_id": "ClDjv3V5", "season_id": "190",
    },
    "UECL": {
        "code": "UECL", "name": "UEFA Avrupa Konferans Ligi", "ulke": "Avrupa",
        "tournament_id": "zcdxsAHq", "tournament_stage_id": "8WHkz50k",
        "tournament_template_id": "GfRbsVWM", "season_id": "190",
    },
    "BL2": {
        "code": "BL2", "name": "2. Bundesliga", "ulke": "Almanya",
        "tournament_id": "tM115Db3", "tournament_stage_id": "6khmdCet",
        "tournament_template_id": "tKH71vSe", "season_id": "190",
    },
    "FL2": {
        "code": "FL2", "name": "Ligue 2 (Fransa)", "ulke": "Fransa",
        "tournament_id": "42ie8pto", "tournament_stage_id": "zX0tEFE6",
        "tournament_template_id": "Y35Jer59", "season_id": "190",
    },
    "UNL": {
        "code": "UNL", "name": "UEFA Uluslar Ligi", "ulke": "Avrupa",
        "tournament_id": "A332sepf", "tournament_stage_id": "A1uGXGgK",
        "tournament_template_id": "A9yxE9Ke", "season_id": "190",
    },
}
for _key, _info in FLASHSCORE_EXTRA_LEAGUES.items():
    FLASHSCORE_TOURNAMENTS[_key] = {
        "tournament_id": _info["tournament_id"],
        "tournament_stage_id": _info["tournament_stage_id"],
    }

# Kilit oyuncu (lig top skorerleri arasındaki) kadroda yoksa o takımın hücum
# gücüne uygulanacak çarpan (1.0 = etkisiz; 0.85 = %15 hücum gücü düşüşü).
KEY_PLAYER_MISSING_ATTACK_MULT = 0.85
KEY_PLAYER_TOP_N = 2  # ligin ilk N golcüsünden biri eksikse etkiyi uygula

ENRICHMENT_WINDOW_HOURS = 30   # bu saatten daha yakın başlayacak maçlara hakem/stad/kadro çekilir
ENRICHMENT_MAX_MATCHES = 10    # bir çalıştırmada en fazla bu kadar maça FlashScore detay çağrısı yapılır
LINEUP_WINDOW_HOURS = 3        # kadro genelde maça çok yakın yayınlanır; bu saatten yakınsa kadro da çekilir
# NOT: RapidAPI ücretsiz planı AYDA 500 istek veriyor. Günde 3 çalıştırma x
# ~10-15 istek/çalıştırma ile bu sınıra yaklaşılabilir; aşılırsa FlashScore
# çağrıları 429 ile başarısız olur ve kod bunu zaten yakalayıp o alanları
# "-" ile sessizce atlar (hiçbir şey çökmez) -- ay sonunda sıfırlanır.

# ---- Open-Meteo (ücretsiz, key gerektirmiyor) -- hava durumu ----
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Elo modeli parametreleri
ELO_START = 1500
ELO_K = 20
ELO_HOME_ADVANTAGE = 60  # ev sahibine eklenen sanal puan
# Elo'nun 1X2 tahminine katkı ağırlığı (0 = hiç katkısı yok/eski davranış,
# 1 = tamamen Elo). Walk-forward backtest'te (10 lig, 132 test maçı) 0.0'dan
# 0.6-0.7'ye kadar log-loss/Brier sürekli iyileşiyor (1X2 log-loss 1.2478 ->
# ~1.05), ama favori-isabet oranı 0.6-0.7'de tepe yapıp 0.9-1.0'da (saf Elo'ya
# yaklaşınca) %40.9'a kadar düşüyor -- yani körü körüne en iyi log-loss'u
# vermeyip Poisson'u (asıl gol modeli, diğer TÜM pazarların -- 2.5/1.5 gol,
# KG, en olası skor -- temeli) baskın tutan ölçülü bir değer seçildi.
ELO_BLEND_WEIGHT = 0.30

# Poisson modeli parametreleri
RECENT_MATCHES_WINDOW = 10  # form hesaplarken bakılacak son maç sayısı
MIN_MATCHES_FOR_MODEL = 8   # bir ligde tahmin üretmek için gereken min. bitmiş maç

# ---- 2.5 Üst/Alt ve KG Var/Yok pazarları için Platt-scaling (lojistik
# recalibrasyon) katsayıları ----
# NEDEN: 2026-10-10 tarihli genişletilmiş walk-forward backtest'te (16 lig,
# 647 test maçı -- football-data.org'un 10 ana ligi + FlashScore'un kalıcı
# tarihçesindeki 9 ek lig) bu iki pazarın HAM Poisson olasılıkları kör
# (eşit-olasılık) referanstan bile kötü çıktı: 2.5 Üst/Alt log-loss 0.7040
# (kör: 0.693), KG Var/Yok log-loss 0.7087 (kör: 0.693) -- yani bu iki sütun
# o haliyle yazı-turadan anlamlı farklı değildi.
#
# DOĞRULAMA: aynı 647 maçlık havuz KRONOLOJİK olarak ikiye bölündü (ilk yarı
# FIT, ikinci yarı HELD-OUT -- fit hiç görmedi). Lojistik recalibrasyon
# (p_kalibre = sigmoid(a + b*logit(p_ham))) sadece FIT yarısında bulundu,
# HELD-OUT yarısında test edildi ve gerçekten iyileşti:
#   2.5 Üst/Alt  HELD-OUT: log-loss 0.7162 -> 0.6856, Brier 0.5172 -> 0.4925
#   KG Var/Yok   HELD-OUT: log-loss 0.7288 -> 0.7000, Brier 0.5314 -> 0.5064
# b katsayılarının ikisi de ~0.46-0.49 (1'in altında) çıkması, modelin bu iki
# pazarda SİSTEMATİK OLARAK fazla-güvenli (overconfident) olduğunu doğruluyor
# -- recalibrasyon uç olasılıkları 0.5'e doğru çekiyor.
#
# Bu sabitler, kalıcı bir model parametresi değil, belirli bir tarihteki veri
# setinden türetilmiş bir düzeltme -- veri havuzu büyüdükçe (match_history
# büyüdükçe, yeni sezon başladıkça) periyodik olarak backtest ile yeniden
# türetilmesi gerekir. models/poisson_model.py predict() içinde SADECE
# "2.5_gol" ve "kg" çıktılarına uygulanır; skor matrisinin kendisi (en olası
# skorlar, 1.5 gol, ilk yarı) etkilenmez -- backtest'te 1.5 Üst/Alt zaten iyi
# kalibre çıkmıştı (log-loss 0.56), dokunulmadı.
OU25_CALIB_A = 0.0624
OU25_CALIB_B = 0.4564
BTTS_CALIB_A = 0.1330
BTTS_CALIB_B = 0.4889
