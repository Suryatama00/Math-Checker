import os
import json
import importlib
import time
from pathlib import Path


try:
    st = importlib.import_module("streamlit")
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
except ModuleNotFoundError as exc:
    missing_package = exc.name or "required package"
    raise SystemExit(
        f"Missing dependency: {missing_package}. "
        "Install dependencies with: pip install streamlit google-genai"
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
        - Tulis jawaban akhir dengan format: `[nomor] Jawaban: ...`
        """
    )

# -----------------------------
# Ambil / upload foto
# -----------------------------

st.subheader("📷 Foto jawabanmu")

camera_image = st.camera_input("Ambil foto")
uploaded_image = st.file_uploader(
    "Atau upload foto", type=["jpg", "jpeg", "png"]
)

image = camera_image or uploaded_image

# -----------------------------
# Fungsi inti: cek jawaban
# -----------------------------


def build_prompt(answer_key: dict) -> str:
    return f"""
Kamu adalah sistem pembaca tulisan tangan dan pemeriksa jawaban matematika.

Gambar berisi jawaban tulisan tangan seorang siswa untuk beberapa soal
bernomor.

Kunci jawaban guru:
{json.dumps(answer_key, indent=2, ensure_ascii=False)}

Untuk SETIAP nomor pada kunci jawaban di atas:
1. Temukan jawaban akhir siswa untuk nomor tersebut.
2. Bandingkan dengan kunci jawaban guru.
3. Terima bentuk yang secara matematis setara (misalnya 1/2 setara 0.5)
   sebagai benar.
4. Jika tulisan tangan tidak bisa dibaca dengan yakin, ATAU nomor tersebut
   tidak ditemukan di foto, gunakan status "unclear" — jangan menebak.
5. Jangan menuliskan penjelasan, langkah pengerjaan, atau membocorkan
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


def check_math(image_bytes: bytes, mime_type: str, answer_key: dict) -> dict:
    """Kirim foto + kunci jawaban ke Gemini, dapatkan hasil terstruktur.

    Server Gemini kadang membalas 503 (sedang sibuk) yang sifatnya
    sementara, jadi kita coba ulang beberapa kali dengan jeda sebelum
    benar-benar menyerah.
    """

    prompt = build_prompt(answer_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

    max_attempts = 3
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[prompt, image_part],
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
# UI: proses dan tampilkan hasil
# -----------------------------

if image:
    st.image(image, caption="Jawabanmu", width="stretch")

    if st.button("✅ Cek jawabanku", type="primary"):

        mime_type = image.type or "image/jpeg"

        with st.spinner("Membaca dan memeriksa jawaban..."):
            try:
                data = check_math(image.getvalue(), mime_type, answer_key)
            except Exception as e:
                st.error(
                    "Terjadi kesalahan saat memproses foto. "
                    "Coba ambil foto ulang dengan pencahayaan lebih baik."
                )
                st.exception(e)
                st.stop()

        st.subheader("Hasil")

        results = data.get("results", [])

        if not results:
            st.warning("Tidak ada jawaban yang terbaca dari foto ini.")
        else:
            n_correct = sum(1 for r in results if r["status"] == "correct")
            n_total = len(results)

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

            st.info(f"Skor: {n_correct} / {n_total} benar")
