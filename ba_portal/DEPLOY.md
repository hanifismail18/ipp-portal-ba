# Deploy gratis ke cloud (tanpa kartu kredit): HF Spaces + Neon + Backblaze B2
Detail pilihan: `plan/DEPLOY_PLAN.md`. Siapkan 3 akun, lalu ikuti 12 langkah ini (sekitar 30 menit).

1. Buat akun https://huggingface.co (gratis). Buka Settings > Access Tokens > New token, tipe **Write**. Simpan.
2. Buat Space: https://huggingface.co/new-space, nama `ba-portal`, SDK **Docker** (Blank), hardware **CPU basic (free)**, visibility **Public**.
3. Buat akun https://neon.tech (login Google/GitHub). Buat project, region terdekat (Singapore). Salin **connection string** (`postgresql://...?sslmode=require`) = `DATABASE_URL`.
4. Buat akun https://www.backblaze.com/sign-up/cloud-storage (B2, tanpa kartu).
5. B2: **Buckets > Create a Bucket**, nama unik (mis. `ba-portal-pdf`), **Private**. Catat nama bucket dan **Endpoint** (mis. `s3.us-west-004.backblazeb2.com`).
6. B2: **Application Keys > Add a New Application Key**, akses ke bucket itu saja. Salin **keyID** dan **applicationKey** (hanya tampil sekali).
7. Di Space: **Settings > Variables and secrets**. Tambah **Secrets**: `DATABASE_URL`, `S3_ACCESS_KEY_ID` (keyID), `S3_SECRET_ACCESS_KEY` (applicationKey), `ADMIN_TOKEN` (min 8 karakter), `KEY_ENC_SECRET` (teks acak panjang, jangan diganti lagi), dan key LLM: `GEMINI_API_KEY`, `OPENROUTER_API_KEY` (opsional, bisa juga diisi lewat halaman admin).
8. Tambah **Variables**: `S3_BUCKET` (nama bucket), `S3_ENDPOINT_URL` = `https://<endpoint dari langkah 5>`, `S3_REGION` = bagian region dari endpoint (mis. `us-west-004`), `ENGINE=chain`, `PUBLIC_BASE_URL` = `https://<user>-ba-portal.hf.space`.
9. Di laptop: `cd /home/han/Downloads/IPP/_work/ba_portal && deploy/hf_push.sh <user>/ba-portal`. Saat diminta password git, tempel token Write dari langkah 1 (username = nama HF kamu).
10. Tunggu build di tab **Logs** Space (5-10 menit). Status berubah **Running**.
11. Cek: buka `https://<user>-ba-portal.hf.space/api/health`. Harus ada `"ok"`. Admin: `.../admin`, masuk dengan `ADMIN_TOKEN`.
12. Uji: upload satu PDF BA, tunggu selesai, buka link `/f/<token>` dari jendela incognito. Share link portal ke rekan.

Catatan: Space tidur setelah 48 jam tanpa pengunjung (bangun ~1 menit). Update kode: ulangi langkah 9. Jangan hapus `KEY_ENC_SECRET`.

---

# Deploy Portal BA (untuk dipakai rekan kerja)

## Cara dapat key (isi lalu jalankan `scripts/set_keys.sh`)
**Gemini (Google AI Studio)**
1. Buka https://aistudio.google.com/apikey dan login dengan akun Google.
2. Klik **Create API key** (pilih/buat project). Supaya tidak cepat kena limit, aktifkan billing di project itu.
3. Salin key (diawali `AIza...`).

**OpenRouter**
1. Buka https://openrouter.ai dan daftar/login.
2. Buka https://openrouter.ai/keys, klik **Create Key**, salin key (diawali `sk-or-...`).
3. (Opsional) Top up di https://openrouter.ai/credits. Model `:free` tetap jalan tanpa kredit, tapi limitnya kecil.

Lalu jalankan:
```
cd /home/han/Downloads/IPP/_work/ba_portal
scripts/set_keys.sh
```
Script menanyakan kedua key (tidak tampil di layar; Enter = tidak diubah), menyimpannya ke `.env`, lalu me-restart portal dan menampilkan status engine.

## Status sekarang (2026-11-02)
- Portal jalan di laptop ini: http://localhost:8080. Admin: http://localhost:8080/admin.
- URL publik sementara lewat ngrok (akun ngrok gratis yang sudah login di laptop ini). URL ada di `PUBLIC_BASE_URL` di `.env` dan di `logs/tunnel.log`.
  - URL ini hanya hidup selama laptop menyala dan proses `ngrok` jalan.
  - ngrok gratis menampilkan halaman peringatan "You are about to visit…" sekali per browser. Klik **Visit Site**.

## Engine LLM (v3: TANPA BIAYA, hanya free tier, `ENGINE=chain`)
1. **Gemini langsung** (key `gemini`, lalu key kedua `gemini2` dari project Google lain). Model `gemini-3.5-flash-lite` dulu, lalu model flash gratis lain.
2. **OpenRouter** (key `openrouter`). Hanya model berakhiran `:free`; model berbayar ditolak oleh kode.
3. **Groq** (key `groq`, free tier).
4. **9router** (gateway lokal): bergiliran mencoba model Gemini free + OpenRouter `:free`. Provider berbayar (deepseek, dll.) ditolak.

`claude -p` tidak dipakai (menghabiskan kuota Claude Boss). Hanya aktif kalau `ALLOW_CLAUDE_CLI=1`.
Kena 429: model itu diistirahatkan dulu, coba model/langkah berikutnya. Kena 402 (minta bayar): langkah itu dianggap habis dan dilewati 1 jam. Tiap langkah punya batas panggilan harian (`*_MAX_CALLS_DAY`).
Kalau semua langkah penuh: BA berstatus **ditunda** ("Antrean, layanan sedang penuh, coba lagi otomatis") dan dicoba ulang otomatis (2, 4, 8 … maks 60 menit). BA tidak pernah gagal permanen karena ini, dan tidak ada biaya.

**Isi key:** paling mudah lewat halaman admin (key manager): key disimpan terenkripsi, langsung dipakai tanpa restart, setelah disimpan hanya terlihat `••••1234`. Alternatif: `scripts/set_keys.sh` (menulis ke `.env`, lalu restart).

Cek status: `curl localhost:8080/api/health`. Field `engines` menunjukkan status per langkah: `ok`, `missing-key` (key belum diisi), `limited` (cooldown 429 / habis 402), `daily-cap` (batas harian tercapai), `down`, `disabled` (claude).

## Yang harus Boss isi di `.env`
Setelah mengisi, restart portal.

| Variabel | Isi | Wajib? |
|---|---|---|
| `ADMIN_TOKEN` | Kode akses admin (minimal 8 karakter). Sekarang `@admin321` (keputusan Boss). | Ya |
| `GEMINI_API_KEY` | Key dari AI Studio (lihat "Cara dapat key"). Isi lewat `scripts/set_keys.sh`. | Sangat disarankan |
| `OPENROUTER_API_KEY` | Key dari OpenRouter (lihat "Cara dapat key"). | Disarankan |
| `LLM_*`, `LLM2_*` | Setting 9router (cadangan). Biarkan seperti sekarang. | Tidak |
| `PUBLIC_BASE_URL` | URL publik (ngrok, domain sendiri, dll). Dipakai untuk link `/f/<token>`. | Ya, kalau dipakai di luar laptop |
| `CLAUDE_FALLBACK` | `1` = boleh memakai Claude sebagai cadangan terakhir, `0` = tidak. | Opsional |

## Cara restart
```
cd /home/han/Downloads/IPP/_work/ba_portal
kill $(ps -eo pid,args | awk '/[u]vicorn server:app/{print $1}')
nohup ./run.sh > logs/server.log 2>&1 &
```

## Pilihan URL publik (pilih salah satu)
| Opsi | Biaya | Kelebihan / kekurangan |
|---|---|---|
| `deploy/tunnel.sh ngrok` (aktif sekarang) | Gratis | Langsung jalan. Ada halaman peringatan ngrok. Laptop harus menyala. |
| `deploy/tunnel.sh cloudflared` | Gratis, tanpa akun | Perlu `sudo dnf install cloudflared`. URL `*.trycloudflare.com` berubah setiap restart. |
| `deploy/tunnel.sh tailscale` | Gratis | Hanya untuk rekan yang ikut tailnet Boss (perlu login Tailscale). |
| Server sendiri + domain + Caddy (`deploy/Caddyfile.example`) | VPS ±Rp80–150rb/bulan + domain ±Rp150rb/tahun | Permanen, HTTPS otomatis, tidak bergantung laptop. Perlu keputusan dan pembayaran dari Boss. |

## Supaya jalan otomatis setelah reboot (opsional)
```
mkdir -p ~/.config/systemd/user && cp deploy/ba-portal.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ba-portal
loginctl enable-linger $USER
```
Matikan dulu proses `nohup` yang sedang jalan supaya port 8080 tidak bentrok.

## Pengaman yang sudah aktif
- **Batas upload:** kuota upload per IP per hari (`QUOTA_PER_CLIENT_PER_DAY`), kuota global per hari, batas call LLM per hari (`MAX_LLM_CALLS_PER_DAY`), antrean maksimal (`MAX_QUEUE`), ukuran file maksimal 20MB, maksimal 40 halaman.
- **Validasi file:** hanya PDF (dicek dari isi file, bukan dari nama), nama file dibersihkan.
- **Header keamanan:** CSP, nosniff, frame-ancestors. Link publik diberi noindex. CORS tertutup.
- **Admin:** login admin dibatasi jumlah percobaannya. Admin bisa mencabut link (`POST /api/admin/revoke/{id}`).
- **Database dan job:** SQLite WAL + busy_timeout. Job yang terputus otomatis diantrekan lagi saat restart.
- **Log:** dirotasi (5MB). Key dan token tidak pernah ditulis ke log.
- **Pembersihan:** upload yang tidak disubmit dihapus otomatis setelah `RETENTION_DAYS` hari (14).
- **Penghematan biaya:** file yang sama tidak diproses ulang (0 call). Pemakaian LLM bisa dilihat di `/api/admin/usage`.

## Hasil uji beban (tests/load_test.md)
- **Upload:** 25 upload bersamaan (20 file sama dan 5 file berbeda) menghasilkan 25 respons 202 dalam 1 detik dan hanya 6 BA unik.
- **Pemrosesan:** keenam BA selesai dalam 38 detik dengan 2 worker.
- **Biaya:** 6 call LLM. File duplikat tidak memakai call sama sekali.
