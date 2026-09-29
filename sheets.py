"""
Modul untuk koneksi dan baca/tulis data ke Google Sheets, dipakai sebagai
"database" hasil pengecekan jawaban siswa.

Struktur spreadsheet (3 tab):

  Siswa      -> Kelas, Nama
  Hasil_Log  -> Timestamp, Kelas, Nama, Aktivitas, Benar, Unclear, Salah
                (tiap kolom Benar/Unclear/Salah berisi nomor dipisah koma,
                 mewakili HASIL ATTEMPT INI SAJA, bukan akumulasi)
  Progress   -> Kelas, Nama, Aktivitas, MasteredNumbers, Benar, Total,
                Persentase, LastUpdated
                (satu baris per siswa+aktivitas, MasteredNumbers = semua
                 nomor yang PERNAH benar di attempt manapun -> ini nilai
                 resminya, terus di-update/upsert tiap ada foto baru)
"""

import os
import datetime
import importlib

try:
    st = importlib.import_module("streamlit")
    gspread = importlib.import_module("gspread")
    Credentials = importlib.import_module(
        "google.oauth2.service_account"
    ).Credentials
except ModuleNotFoundError as exc:
    missing_package = exc.name or "required package"
    raise SystemExit(
        f"Missing dependency: {missing_package}. "
        "Install dependencies with: pip install gspread google-auth"
    ) from exc


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

SISWA_HEADERS = ["Kelas", "Nama"]
LOG_HEADERS = [
    "Timestamp", "Kelas", "Nama", "Aktivitas", "Benar", "Unclear", "Salah",
]
PROGRESS_HEADERS = [
    "Kelas", "Nama", "Aktivitas", "MasteredNumbers",
    "Benar", "Total", "Persentase", "LastUpdated",
]


def _get_service_account_info() -> dict:
    try:
        return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass

    local_path = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
    )
    if os.path.isfile(local_path):
        import json
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(
        "Kredensial Google Service Account tidak ditemukan. "
        "Isi Streamlit secrets [gcp_service_account] atau sediakan "
        "file service_account.json di folder project (untuk lokal)."
    )


def _get_spreadsheet_id() -> str:
    try:
        return st.secrets["SPREADSHEET_ID"]
    except Exception:
        pass

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise RuntimeError(
            "SPREADSHEET_ID belum diset. Isi lewat Streamlit secrets "
            "atau file .env."
        )
    return spreadsheet_id


@st.cache_resource(show_spinner=False)
def get_spreadsheet():
    info = _get_service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(_get_spreadsheet_id())


def _get_or_create_worksheet(sh, name: str, headers: list):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

    if not ws.get_all_values():
        ws.append_row(headers)

    return ws


@st.cache_data(ttl=60, show_spinner=False)
def get_roster() -> dict:
    sh = get_spreadsheet()
    ws = _get_or_create_worksheet(sh, "Siswa", SISWA_HEADERS)
    records = ws.get_all_records()

    roster = {}
    for row in records:
        kelas = str(row.get("Kelas", "")).strip()
        nama = str(row.get("Nama", "")).strip()
        if not kelas or not nama:
            continue
        roster.setdefault(kelas, []).append(nama)

    return roster


def _sort_key(n: str):
    """Urutkan nomor secara alami: 1,2,...,10 lalu 1a,1b jika ada huruf."""
    try:
        return (0, int(n), "")
    except ValueError:
        digits = "".join(ch for ch in n if ch.isdigit())
        letters = "".join(ch for ch in n if not ch.isdigit())
        return (1, int(digits) if digits else 0, letters)


def _find_progress_row(ws, kelas: str, nama: str, activity_title: str):
    """Cari baris progress yang sudah ada untuk siswa+aktivitas ini.
    Return (nomor_baris_di_sheet, dict_baris) atau (None, None)."""

    values = ws.get_all_values()
    if len(values) < 2:
        return None, None

    header = values[0]
    for i, row in enumerate(values[1:], start=2):
        row_dict = dict(zip(header, row))
        if (
            row_dict.get("Kelas") == kelas
            and row_dict.get("Nama") == nama
            and row_dict.get("Aktivitas") == activity_title
        ):
            return i, row_dict

    return None, None


def save_results(
    kelas: str,
    nama: str,
    activity_title: str,
    results: list,
) -> dict:
    """Simpan hasil attempt ke Hasil_Log, lalu update mastery kumulatif
    di Progress. Mengembalikan ringkasan nilai KUMULATIF (bukan cuma
    attempt ini saja)."""

    sh = get_spreadsheet()
    log_ws = _get_or_create_worksheet(sh, "Hasil_Log", LOG_HEADERS)
    progress_ws = _get_or_create_worksheet(sh, "Progress", PROGRESS_HEADERS)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    attempt_correct = {
        str(r["number"]) for r in results if r["status"] == "correct"
    }
    attempt_unclear = {
        str(r["number"]) for r in results if r["status"] == "unclear"
    }
    attempt_incorrect = {
        str(r["number"]) for r in results if r["status"] == "incorrect"
    }

    log_ws.append_row([
        timestamp,
        kelas,
        nama,
        activity_title,
        ", ".join(sorted(attempt_correct, key=_sort_key)),
        ", ".join(sorted(attempt_unclear, key=_sort_key)),
        ", ".join(sorted(attempt_incorrect, key=_sort_key)),
    ])

    row_num, existing = _find_progress_row(
        progress_ws, kelas, nama, activity_title
    )

    previous_mastered = set()
    if existing and existing.get("MasteredNumbers"):
        previous_mastered = {
            n.strip() for n in existing["MasteredNumbers"].split(",")
            if n.strip()
        }

    newly_mastered = attempt_correct - previous_mastered
    all_mastered = previous_mastered | attempt_correct
    mastered_sorted = sorted(all_mastered, key=_sort_key)

    total = len(results)
    n_correct = len(mastered_sorted)
    percentage = round((n_correct / total) * 100, 1) if total else 0

    row_values = [
        kelas,
        nama,
        activity_title,
        ", ".join(mastered_sorted),
        n_correct,
        total,
        percentage,
        timestamp,
    ]

    if row_num:
        progress_ws.update(f"A{row_num}:H{row_num}", [row_values])
    else:
        progress_ws.append_row(row_values)

    remaining = sorted(
        {str(r["number"]) for r in results} - all_mastered, key=_sort_key
    )

    return {
        "correct": n_correct,
        "total": total,
        "percentage": percentage,
        "newly_mastered": sorted(newly_mastered, key=_sort_key),
        "remaining": remaining,
    }
