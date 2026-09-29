"""
Modul untuk koneksi dan baca/tulis data ke Google Sheets, dipakai sebagai
"database" hasil pengecekan jawaban siswa.

Struktur spreadsheet yang diharapkan (3 tab / worksheet):

  Siswa            -> kolom: Kelas, Nama
  Hasil_Detail     -> kolom: Timestamp, Kelas, Nama, Aktivitas, Nomor, Status
  Hasil_Ringkasan  -> kolom: Timestamp, Kelas, Nama, Aktivitas, Benar, Total, Persentase
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
DETAIL_HEADERS = ["Timestamp", "Kelas", "Nama", "Aktivitas", "Nomor", "Status"]
RINGKASAN_HEADERS = [
    "Timestamp", "Kelas", "Nama", "Aktivitas", "Benar", "Total", "Persentase",
]


def _get_service_account_info() -> dict:
    """Ambil kredensial service account dari Streamlit secrets (cloud)
    atau dari file lokal service_account.json (untuk testing lokal)."""

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
    """Buka spreadsheet Google Sheets, cache supaya tidak login ulang
    setiap kali fungsi dipanggil."""

    info = _get_service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(_get_spreadsheet_id())


def _get_or_create_worksheet(sh, name: str, headers: list[str]):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

    # Pastikan header ada kalau worksheet baru dibuat manual dan kosong
    if not ws.get_all_values():
        ws.append_row(headers)

    return ws


@st.cache_data(ttl=60, show_spinner=False)
def get_roster() -> dict:
    """Ambil daftar Kelas -> [Nama, ...] dari tab 'Siswa'.

    Di-cache 60 detik supaya tidak nge-hit Google Sheets API setiap
    kali dropdown dirender, tapi tetap cukup responsif kalau guru baru
    saja menambah nama.
    """

    sh = get_spreadsheet()
    ws = _get_or_create_worksheet(sh, "Siswa", SISWA_HEADERS)
    records = ws.get_all_records()

    roster: dict[str, list[str]] = {}
    for row in records:
        kelas = str(row.get("Kelas", "")).strip()
        nama = str(row.get("Nama", "")).strip()
        if not kelas or not nama:
            continue
        roster.setdefault(kelas, []).append(nama)

    return roster


def save_results(
    kelas: str,
    nama: str,
    activity_title: str,
    results: list[dict],
) -> dict:
    """Simpan hasil pengecekan ke tab Hasil_Detail dan Hasil_Ringkasan.

    Mengembalikan dict berisi jumlah benar, total, dan persentase.
    """

    sh = get_spreadsheet()
    detail_ws = _get_or_create_worksheet(sh, "Hasil_Detail", DETAIL_HEADERS)
    ringkasan_ws = _get_or_create_worksheet(
        sh, "Hasil_Ringkasan", RINGKASAN_HEADERS
    )

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    detail_rows = [
        [timestamp, kelas, nama, activity_title, r["number"], r["status"]]
        for r in results
    ]
    detail_ws.append_rows(detail_rows)

    n_correct = sum(1 for r in results if r["status"] == "correct")
    n_total = len(results)
    percentage = round((n_correct / n_total) * 100, 1) if n_total else 0

    ringkasan_ws.append_row(
        [timestamp, kelas, nama, activity_title, n_correct, n_total, percentage]
    )

    return {"correct": n_correct, "total": n_total, "percentage": percentage}
