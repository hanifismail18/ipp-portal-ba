# Portal Submit BA Kekurangan Gaji HL

## Menjalankan
```
cd /home/han/Downloads/IPP/_work/ba_portal
./run.sh                      # buka http://localhost:8080 (admin: http://localhost:8080/admin)
```
- `run.sh` membaca `.env` (kalau belum ada, disalin dari `.env.example`). Kalau `LLM_BASE_URL` mengarah ke 9router, 9router otomatis dinyalakan.
- Engine LLM diatur lewat `ENGINE` di `.env`:
  - `claude`: memakai CLI `claude -p` (sonnet) dari _work/ba_tool, tanpa API key.
  - `openai`: memakai endpoint OpenAI-compatible, yaitu 9router (`http://127.0.0.1:20128/v1`, model `gemini/...`) atau Gemini langsung (`https://generativelanguage.googleapis.com/v1beta/openai`).
  - `auto`: pakai `openai` kalau `LLM_*` sudah diisi.
- Cek status: `curl localhost:8080/api/health` (hasilnya `llm: ok|down`, engine, panjang antrean).

## Admin (hasil submit)
- Halaman `/admin`: masukkan `ADMIN_TOKEN` dari `.env`, lalu lihat daftar submit atau download CSV.
- Lewat curl: `curl -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8080/api/admin/export.csv -o rekap.csv`
- Pemakaian LLM (jumlah call/token per BA): `GET /api/admin/usage` dengan header yang sama.

## Kontrol biaya
- File yang sama (sha256 sama) memakai hasil lama, jadi 0 call LLM.
- Text layer dipakai lebih dulu. Halaman yang hanya teks tidak dikirim sebagai gambar.
- Gambar maksimal 1600px, JPEG q80, maksimal 8 halaman (`PAGE_CAP`).
- 1 call per BA. Kalau kena 429/503 ada retry dengan backoff dan fallback model (`LLM_MODEL` dipisah koma).
- Batasan: antrean global (`LLM_CONCURRENCY`), kuota upload per IP per hari (`QUOTA_PER_CLIENT_PER_DAY`), dan batas call LLM per hari (`MAX_LLM_CALLS_PER_DAY`).
- Scan/foto miring atau kusam diperbaiki otomatis (deskew, kontras). Kalau tetap buram, muncul alert "Foto/scan kurang jelas, upload ulang" dan BA tidak bisa disubmit.

## Deploy untuk banyak user
1. Jalankan di belakang reverse proxy (nginx/caddy, HTTPS) dan set `TRUST_PROXY=1` supaya kuota dihitung per IP asli.
2. Satu proses uvicorn sudah cukup karena pekerjaan berat masuk antrean dan dibatasi `LLM_CONCURRENCY`. Untuk skala lebih besar, pindahkan DB ke Postgres (skema portabel, lihat `db.py`) dan antrean ke Redis/RQ dengan beberapa worker.
3. Pakai API key Gemini berbayar (key free tier sering kena 429). Naikkan `LLM_CONCURRENCY` sesuai limit RPM key tersebut.
4. Backup `data/` (berisi DB dan PDF upload). Jangan commit `.env`.
