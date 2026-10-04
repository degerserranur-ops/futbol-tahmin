# Futbol Maç Tahminleme Sistemi

İstatistiksel bir maç analiz/tahmin aracı. Geçmiş sonuçlardan takımların
hücum/savunma gücünü çıkarır (Poisson gol modeli) ve ayrıca bir Elo
derecelendirmesi tutar; bu ikisini birleştirerek bir maç için 1X2, 2.5
üst/alt, KG var/yok olasılıklarını ve en olası skorları üretir. Sonuçları
hem komut satırında hem de hazır bir Excel tablosu olarak alabilirsin.

**Not:** Bu araç analiz ve eğlence amaçlıdır; kesin sonuç garanti etmez,
bahis tavsiyesi değildir.

## Nasıl çalışır?

1. **Poisson gol modeli** (`models/poisson_model.py`): Her takımın attığı/
   yediği gol sayısını lig ortalamasına göre normalize ederek bir
   "hücum katsayısı" ve "savunma katsayısı" çıkarır. İki takım
   karşılaştığında beklenen gol sayıları (λ) hesaplanır ve Poisson
   dağılımıyla 0-9 gol için bir skor olasılık matrisi kurulur. Bu
   matristen 1X2, üst/alt, KG gibi tüm olasılıklar toplanarak çıkarılır.
   Model, HER TURNUVA için AYRI eğitilir (Premier League'in gol ortalaması
   La Liga'dan farklı olabileceği için ligler birbirine karıştırılmaz).

2. **Elo derecelendirmesi** (`models/elo.py`): Her maç sonucunda takımların
   puanı klasik Elo formülüyle güncellenir (ev sahibi avantajı ve gol
   farkı ağırlıklı). Referans amaçlı, Poisson'dan bağımsız bir "ikinci
   görüş" sağlar.

3. **Veri katmanı** (`data/football_data_client.py`): football-data.org'dan
   (ücretsiz, sonsuza dek) maç verisi çeker ve diske cache'ler.

## Veri kaynağı: football-data.org

**Kapsam (free tier, sonsuza dek ücretsiz):** Premier League, La Liga,
Serie A, Bundesliga, Ligue 1, UEFA Şampiyonlar Ligi, Eredivisie, Primeira
Liga, Championship, Dünya Kupası, Avrupa Şampiyonası, Brezilya Serie A.

**Türkiye Süper Lig bu kaynakta YOK.** Araştırdığımız ücretsiz
alternatiflerin hiçbiri Süper Lig'i hem güncel sezon hem de makul
limitlerle sunmuyordu (API-Football ücretsiz planı Süper Lig'i içeriyor
ama güncel sezonun toplu verisine kapalı, sadece eski sezonlar (2022-2024)
veya "dün-bugün-yarın" gibi 3 günlük dar bir pencereye izin veriyor —
bu yüzden `data/api_client.py` ve `predict.py` dosyaları projede duruyor
ama varsayılan olarak kullanılmıyor). Süper Lig'i eklemek istersen
API-Football'un ücretli planına geçmek gerekir.

### Kurulum

```bash
cd futbol-tahmin
pip install -r requirements.txt
```

1. https://www.football-data.org/client/register üzerinden ücretsiz kayıt ol
   (sadece e-posta + şifre).
2. Hesabından aldığın token'ı `.env` dosyasına ekle:
   ```
   FOOTBALL_DATA_TOKEN=senin_token_in
   ```
3. Dakikalık istek limiti var (~10/dk); `export_excel.py` istekler arasına
   kısa bekleme koyar ve cevapları cache'ler (varsayılan 6 saat TTL).

### Anahtar olmadan deneme (demo)

```bash
python demo_data.py
```

## Kullanım

### Excel tablosu (tarih, saat, skor + tahmin bir arada)

```bash
# Varsayılan: son 40 gün + önümüzdeki 10 gün, tüm turnuvalar
python export_excel.py

# Belirli turnuvalar ve farklı tarih aralığı
python export_excel.py --lookback-days 60 --forward-days 14 --competitions premier_league ucl

# Desteklenen turnuva anahtarları: premier_league, la_liga, serie_a,
# bundesliga, ligue_1, ucl  (bkz. config.py -> COMPETITIONS)
```

Çıkan `maclar_ve_tahminler.xlsx` dosyasında her satır bir maç: Tarih, Gün,
Saat (İstanbul saatiyle), Lig, Ev Sahibi, Deplasman, İY Skor, MS Skor,
Durum, Model Tahmini (1/X/2), Ev/Beraberlik/Deplasman %, Beklenen Skor,
2.5 Üst %, KG Var %. Oynanmış maçlar gri renkle, az veri olan turnuvalar
"Yetersiz geçmiş veri" notuyla işaretlenir. İkinci sekmede ("Bilgi")
rapor hakkında notlar var.

### Komut satırından tek maç tahmini (Süper Lig/API-Football, opsiyonel)

```bash
python predict.py --league premier_league --home "Arsenal" --away "Chelsea"
```

## Sınırlamalar

- Model sadece gol istatistiklerine bakar; sakatlık, kart cezası, moral,
  hava durumu, maçın önemi gibi faktörleri bilmez.
- Küçük örneklem (sezon başı, Şampiyonlar Ligi'nin lig fazı gibi az maçlı
  turnuvalar) tahminleri gürültülü yapar; bazı takımların henüz 2-3 maçı
  olabilir, bu durumda o takımın güç tahmini kaba bir yaklaşımdır.
- Türkiye Süper Lig şu an tabloya dahil değil (bkz. yukarıdaki not).
- Bu bir "kesin tahmin" aracı değil, olasılık/istatistik aracıdır.

## Dosya yapısı

```
futbol-tahmin/
├── config.py                    # turnuva kodları, Elo/Poisson parametreleri
├── export_excel.py              # ana araç: Excel tablosu üretir
├── predict.py                   # CLI: tek maç/upcoming tahmini (API-Football, opsiyonel)
├── demo_data.py                 # API anahtarı olmadan test
├── data/
│   ├── football_data_client.py  # cache'li football-data.org istemcisi (AKTİF)
│   └── api_client.py            # cache'li API-Football istemcisi (opsiyonel/Süper Lig)
├── models/
│   ├── elo.py                   # Elo derecelendirme
│   └── poisson_model.py         # Poisson gol modeli (asıl tahmin motoru)
└── cache/                       # API cevaplarının cache'lendiği klasör
```

## Geliştirme fikirleri (opsiyonel)

- Dixon-Coles düzeltmesi (düşük skorlu maçların az olma eğilimini
  modele ekler, özellikle 0-0/1-0 gibi skorlarda doğruluğu artırır).
- Son N maça ağırlık vererek "form" etkisini güncel sonuçlara daha
  fazla önem verecek şekilde modelleme.
- Geçmiş tahminleri gerçek sonuçlarla karşılaştırıp modelin doğruluğunu
  (Brier score, log-loss) ölçen bir backtest script'i.
- Süper Lig için ücretli bir plana geçilirse `predict.py`/`api_client.py`
  zaten hazır; `export_excel.py`'a aynı mantıkla eklenebilir.
