import os
import json
import importlib
import time
from pathlib import Path


try:
    st = importlib.import_module("streamlit")
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
    sheets = importlib.import_module("sheets")
except ModuleNotFoundError as exc:
    missing_package = exc.name or "required package"
    raise SystemExit(
        f"Missing dependency: {missing_package}. "
        "Install dependencies with: pip install streamlit google-genai "
        "gspread google-auth"
    ) from exc

# -----------------------------
# Setup
# -----------------------------

def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"\''))


load_env_file(Path(__file__).with_name(".env"))

def get_api_key():
    try:
        return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY")


API_KEY = get_api_key()

if not API_KEY:
    st.error(
        "GEMINI_API_KEY belum diset. Buat file .env berisi "
        "GEMINI_API_KEY=isi_api_key_kamu"
    )
    st.stop()

client = genai.Client(api_key=API_KEY)

MODEL_NAME = "gemini-3.5-flash-lite"  # cepat, murah/gratis, cukup untuk task ini

# -----------------------------
# Load answer keys
# -----------------------------

with open("answer_keys.json", "r", encoding="utf-8") as f:
    ANSWER_KEYS = json.load(f)

# -----------------------------
# Page config
# -----------------------------

st.set_page_config(
    page_title="Math Self-Check",
    page_icon="✅",
    layout="centered",
)

st.title("📐 Math Self-Check")
st.caption("Foto jawabanmu, langsung tahu mana yang benar dan salah.")

# -----------------------------
# Pilih Kelas & Nama
# -----------------------------

try:
    roster = sheets.get_roster()
except Exception as e:
    st.error(
        "Gagal terhubung ke Google Sheets (database nilai). "
        "Cek SPREADSHEET_ID dan kredensial service account."
    )
    st.exception(e)
    st.stop()

if not roster:
    st.warning(
        "Tab 'Siswa' di Google Sheets masih kosong. Isi dulu kolom "
        "Kelas dan Nama di sana."
    )
    st.stop()

col1, col2 = st.columns(2)

with col1:
    kelas = st.selectbox("Kelas", options=sorted(roster.keys()))

with col2:
    nama = st.selectbox("Nama", options=sorted(roster[kelas]))

st.divider()

# -----------------------------
# Pilih aktivitas
# -----------------------------

session_id = st.selectbox(
    "Pilih aktivitas",
    options=list(ANSWER_KEYS.keys()),
    format_func=lambda k: ANSWER_KEYS[k]["title"],
)

activity = ANSWER_KEYS[session_id]
answer_key = activity["questions"]

st.write(f"**{activity['title']}**")

with st.expander("📋 Tips supaya hasilnya akurat"):
    st.markdown(
        """
        - Pastikan seluruh halaman jawaban masuk ke foto
        - Tulisan harus jelas terbaca, hindari coretan berlebihan
        - Gunakan pencahayaan yang cukup, hindari bayangan
        - Pegang kamera tegak lurus di atas kertas
        - Kalau jawaban kepotong jadi beberapa halaman, foto satu-satu
          lalu klik "Tambah ke antrian" untuk tiap halaman
        """
    )

# -----------------------------
# Fungsi inti: cek jawaban (mendukung banyak gambar sekaligus)
# -----------------------------


def build_prompt(answer_key: dict) -> str:
    return f"""
Kamu adalah sistem pembaca tulisan tangan dan pemeriksa jawaban matematika.

Kamu akan diberi SATU ATAU LEBIH gambar. Semua gambar itu adalah bagian
dari jawaban tulisan tangan SATU siswa yang sama untuk soal-soal
bernomor (jawabannya mungkin tersebar di beberapa gambar/halaman).

Kunci jawaban guru:
{json.dumps(answer_key, indent=2, ensure_ascii=False)}

Untuk SETIAP nomor pada kunci jawaban di atas:
1. Cari jawaban akhir siswa untuk nomor tersebut di SEMUA gambar yang
   diberikan (nomor yang sama tidak akan muncul dobel di lebih dari satu
   gambar, tapi kamu perlu memeriksa semua gambar untuk menemukannya).
2. Bandingkan dengan kunci jawaban guru.
3. Terima bentuk yang secara matematis setara (misalnya 1/2 setara 0.5)
   sebagai benar.
4. Faktor perkalian yang dibolak-balik urutannya (sifat komutatif),
   misalnya (x+2)(x+3)=0 dianggap SAMA dengan (x+3)(x+2)=0 — urutan
   penulisan faktor tidak memengaruhi kebenaran jawaban.
5. Jika tulisan tangan tidak bisa dibaca dengan yakin, ATAU nomor
   tersebut tidak ditemukan di gambar manapun, gunakan status "unclear"
   — jangan menebak.
6. Jangan menuliskan penjelasan, langkah pengerjaan, atau membocorkan
   kunci jawaban ke output.

Kembalikan HANYA JSON dengan struktur berikut, tanpa teks lain:
{{
  "results": [
    {{"number": "1", "status": "correct"}},
    {{"number": "2", "status": "incorrect"}},
    {{"number": "3", "status": "unclear"}}
  ]
}}

Nilai status yang diperbolehkan hanya: correct, incorrect, unclear.
"""


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["correct", "incorrect", "unclear"],
                    },
                },
                "required": ["number", "status"],
            },
        }
    },
    "required": ["results"],
}


def check_math(images: list, answer_key: dict) -> dict:
    """Kirim satu atau lebih foto + kunci jawaban ke Gemini dalam SATU
    panggilan API, dapatkan hasil terstruktur gabungan.

    images: list berisi tuple (image_bytes, mime_type).

    Server Gemini kadang membalas 503 (sedang sibuk) yang sifatnya
    sementara, jadi kita coba ulang beberapa kali dengan jeda sebelum
    benar-benar menyerah.
    """

    prompt = build_prompt(answer_key)
    image_parts = [
        types.Part.from_bytes(data=data, mime_type=mime)
        for data, mime in images
    ]
    contents = [prompt] + image_parts

    max_attempts = 3
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0,
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            last_error = e
            is_last_attempt = attempt == max_attempts
            if is_last_attempt:
                raise
            time.sleep(3 * attempt)

    raise last_error


# -----------------------------
# Antrian foto (session state)
# -----------------------------

if "queue" not in st.session_state:
    st.session_state["queue"] = []  # list of dict: {bytes, mime, name}

if "capture_key" not in st.session_state:
    st.session_state["capture_key"] = 0

if "uploader_key" not in st.session_state:
    st.session_state["uploader_key"] = 0


def _reset_capture_widget():
    st.session_state["capture_key"] += 1


def _add_to_queue(file_obj):
    st.session_state["queue"].append({
        "bytes": file_obj.getvalue(),
        "mime": file_obj.type or "image/jpeg",
        "name": getattr(file_obj, "name", "foto"),
    })


st.subheader("📷 Foto jawabanmu")
st.caption(
    f"Sedang mengisi untuk: **{nama}** ({kelas}) — "
    f"{activity['title']}"
)

tab_camera, tab_upload = st.tabs(["Kamera", "Upload file"])

with tab_camera:
    camera_image = st.camera_input(
        "Ambil foto", key=f"camera_{st.session_state['capture_key']}"
    )
    if camera_image is not None:
        if st.button("➕ Tambah foto ini ke antrian"):
            _add_to_queue(camera_image)
            _reset_capture_widget()
            st.rerun()

with tab_upload:
    uploaded_files = st.file_uploader(
        "Pilih satu atau beberapa foto",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state['uploader_key']}",
    )
    if uploaded_files:
        if st.button("➕ Tambah semua ke antrian"):
            for f in uploaded_files:
                _add_to_queue(f)
            st.session_state["uploader_key"] += 1
            st.rerun()

# -----------------------------
# Tampilkan antrian
# -----------------------------

queue = st.session_state["queue"]

if queue:
    st.write(f"**Antrian foto ({len(queue)}):**")
    cols = st.columns(min(len(queue), 4))
    for i, item in enumerate(queue):
        with cols[i % len(cols)]:
            st.image(item["bytes"], width="stretch")
            if st.button("🗑 Hapus", key=f"remove_{i}"):
                st.session_state["queue"].pop(i)
                st.rerun()

    st.divider()

    check_clicked = st.button(
        f"✅ Cek semua foto ({len(queue)})", type="primary"
    )

    if check_clicked:
        images = [(item["bytes"], item["mime"]) for item in queue]

        with st.spinner("Membaca dan memeriksa jawaban dari semua foto..."):
            try:
                data = check_math(images, answer_key)
            except Exception as e:
                st.error(
                    "Gagal memproses foto setelah beberapa kali percobaan. "
                    "Kemungkinan server Gemini sedang sibuk — coba lagi "
                    "sebentar lagi, atau ambil foto ulang dengan "
                    "pencahayaan lebih baik."
                )
                st.exception(e)
                st.stop()

        st.subheader("Hasil")

        results = data.get("results", [])

        if not results:
            st.warning("Tidak ada jawaban yang terbaca dari foto ini.")
        else:
            for r in sorted(results, key=lambda r: str(r["number"])):
                number = r["number"]
                status = r["status"]

                if status == "correct":
                    st.success(f"Nomor {number}: Benar ✅")
                elif status == "incorrect":
                    st.error(f"Nomor {number}: Salah ❌")
                else:
                    st.warning(
                        f"Nomor {number}: Foto ulang, tulisan tidak "
                        f"terbaca jelas ⚠️"
                    )

            try:
                summary = sheets.save_results(
                    kelas=kelas,
                    nama=nama,
                    activity_title=activity["title"],
                    results=results,
                )
                st.info(
                    f"Nilai (akumulasi): {summary['correct']} / "
                    f"{summary['total']} benar ({summary['percentage']}%) "
                    f"— **{nama}** ({kelas})"
                )
                if summary["remaining"]:
                    st.warning(
                        "Masih perlu diulang nomor: "
                        + ", ".join(summary["remaining"])
                    )
                else:
                    st.success("🎉 Semua nomor sudah pernah benar!")

                # Kosongkan antrian setelah berhasil disimpan, siap untuk
                # siswa berikutnya (Kelas/Nama tetap seperti sebelumnya,
                # tinggal diganti kalau lanjut ke siswa lain).
                st.session_state["queue"] = []

            except Exception as e:
                st.error(
                    "Hasil berhasil dibaca, tapi GAGAL disimpan ke "
                    "Google Sheets. Screenshot hasil ini dulu untuk "
                    "jaga-jaga, lalu laporkan ke guru."
                )
                st.exception(e)
else:
    st.caption(
        "Belum ada foto di antrian. Ambil/upload foto lalu klik "
        "tombol \"Tambah ke antrian\" di atas."
    )
