"""
maclar_ve_tahminler.xlsx içindeki güncel veriyi Google Sheets'e aktarır.

GitHub Actions (veya herhangi bir CI ortamı) içinde kimlik doğrulama için
Claude'un kullandığı OAuth bağlantısı yerine bir Google servis hesabı
kullanır -- bu yüzden export_excel.py'den tamamen ayrı, bağımsız bir script.

Kullanım:
    GOOGLE_SERVICE_ACCOUNT_JSON=... SPREADSHEET_ID=... \
        python push_to_sheets.py [--xlsx maclar_ve_tahminler.xlsx]

Gereken ortam değişkenleri:
    GOOGLE_SERVICE_ACCOUNT_JSON  -- servis hesabı key dosyasının TAM İÇERİĞİ (JSON metni)
    SPREADSHEET_ID                -- hedef Google Sheet ID (varsayılan: aşağıdaki sabit)

Sheet yapısı (CLAUDE.md / export_excel.py ile birebir aynı tutulmalı):
    - "Sayfa1" sekmesi: 2 sabit başlık satırı, veri 3. satırdan başlar, 35 sütun (A:AI).
    - "Öne Çıkanlar" sekmesi: başlık satırı + gün ayraç satırları + seçim satırları (A:I).
    ŞEMA DEĞİŞTİRİLMEZ -- bu script SADECE veri satırlarını temizleyip yeniden yazar,
    hiçbir sütun eklemez/çıkarmaz, başlık satırlarına dokunmaz.
"""
import argparse
import json
import os
import sys

from openpyxl import load_workbook

DEFAULT_SPREADSHEET_ID = "1bujfpBCV9EqBultlLCZUF1UNtciptc0wS86xrKzE-nw"
MAIN_SHEET_NAME = "Sayfa1"
MAIN_SHEET_DATA_START_ROW = 3  # 1-2: sabit başlık satırları
MAIN_SHEET_LAST_COL = "AI"     # 35 sütun (A..AI)
MAIN_SHEET_CLEAR_END_ROW = 1000

HIGHLIGHT_SHEET_NAME = "Öne Çıkanlar"
HIGHLIGHT_SHEET_LAST_COL = "I"  # 9 sütun (A..I)
HIGHLIGHT_SHEET_CLEAR_END_ROW = 250


def _get_sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        print("HATA: GOOGLE_SERVICE_ACCOUNT_JSON ortam değişkeni boş/yok.", file=sys.stderr)
        sys.exit(1)
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def _read_xlsx(xlsx_path: str):
    wb = load_workbook(xlsx_path, data_only=True)

    ws_main = wb["Maçlar ve Tahminler"]
    main_rows = [list(row) for row in ws_main.iter_rows(min_row=3, values_only=True)]
    # Sondaki tamamen boş satırları at (openpyxl bazen fazladan boş satır döndürür)
    while main_rows and all(v is None for v in main_rows[-1]):
        main_rows.pop()

    ws_hl = wb[HIGHLIGHT_SHEET_NAME]
    hl_rows = [list(row) for row in ws_hl.iter_rows(min_row=1, values_only=True)]
    while hl_rows and all(v is None for v in hl_rows[-1]):
        hl_rows.pop()

    return main_rows, hl_rows


def _clear_range(service, spreadsheet_id, rng):
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=rng, body={}
    ).execute()


def _write_range(service, spreadsheet_id, rng, values):
    if not values:
        return
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def _reset_basic_filter(service, spreadsheet_id, new_row_count):
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title),basicFilter)",
    ).execute()
    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") != MAIN_SHEET_NAME:
            continue
        sheet_id = props["sheetId"]
        existing_filter = sheet.get("basicFilter")
        if not existing_filter:
            return  # filtre yoksa dokunma, yaratma
        new_range = dict(existing_filter["range"])
        new_range["sheetId"] = sheet_id
        new_range["startRowIndex"] = MAIN_SHEET_DATA_START_ROW - 1  # 0-indexed
        new_range["endRowIndex"] = MAIN_SHEET_DATA_START_ROW - 1 + new_row_count
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"setBasicFilter": {"filter": {"range": new_range}}}]},
        ).execute()
        return


def main():
    parser = argparse.ArgumentParser(description="xlsx'teki veriyi Google Sheets'e aktar")
    parser.add_argument("--xlsx", default="maclar_ve_tahminler.xlsx")
    parser.add_argument("--spreadsheet-id", default=os.environ.get("SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID))
    args = parser.parse_args()

    service = _get_sheets_service()
    main_rows, hl_rows = _read_xlsx(args.xlsx)
    print(f"Ana tablo: {len(main_rows)} satır, Öne Çıkanlar: {len(hl_rows)} satır")

    # --- Sayfa1: temizle + yaz ---
    _clear_range(
        service, args.spreadsheet_id,
        f"{MAIN_SHEET_NAME}!A{MAIN_SHEET_DATA_START_ROW}:{MAIN_SHEET_LAST_COL}{MAIN_SHEET_CLEAR_END_ROW}",
    )
    if main_rows:
        end_row = MAIN_SHEET_DATA_START_ROW - 1 + len(main_rows)
        _write_range(
            service, args.spreadsheet_id,
            f"{MAIN_SHEET_NAME}!A{MAIN_SHEET_DATA_START_ROW}:{MAIN_SHEET_LAST_COL}{end_row}",
            main_rows,
        )

    # --- Öne Çıkanlar: temizle + yaz (başlık + gün ayraçları dahil, A1'den) ---
    _clear_range(
        service, args.spreadsheet_id,
        f"{HIGHLIGHT_SHEET_NAME}!A1:{HIGHLIGHT_SHEET_LAST_COL}{HIGHLIGHT_SHEET_CLEAR_END_ROW}",
    )
    if hl_rows:
        end_row = len(hl_rows)
        _write_range(
            service, args.spreadsheet_id,
            f"{HIGHLIGHT_SHEET_NAME}!A1:{HIGHLIGHT_SHEET_LAST_COL}{end_row}",
            hl_rows,
        )

    _reset_basic_filter(service, args.spreadsheet_id, len(main_rows))
    print("Google Sheets güncellendi.")


if __name__ == "__main__":
    main()
