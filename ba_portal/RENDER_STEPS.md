# Deploy ke Render (gratis)
1. Buka https://dashboard.render.com, login (GitHub).
2. New > Blueprint > pilih repo `hanifismail18/ipp-portal-ba` > Connect.
3. Di terminal lokal: `cd _work/ba_portal && bash scripts/render_env.sh` (menampilkan nilai env; jangan dibagikan).
4. Render meminta nilai env (sync:false). Tempel satu per satu:
   - DATABASE_URL: dari `.env.deploy`
   - S3_ENDPOINT_URL: dari `.env.deploy` (S3_ENDPOINT)
   - S3_ACCESS_KEY_ID: dari `.env.deploy` (B2_KEY_ID)
   - S3_SECRET_ACCESS_KEY: dari `.env.deploy` (B2_APP_KEY)
   - S3_BUCKET, S3_REGION: dari `.env.deploy`
   - ADMIN_TOKEN, KEY_ENC_SECRET: dari `.env` (KEY_ENC_SECRET wajib tetap sama, kalau hilang API key admin harus diinput ulang)
   - PUBLIC_BASE_URL: kosongkan dulu
5. Klik Apply; tunggu build selesai (~5-10 menit).
6. Salin URL layanan (https://....onrender.com).
7. Service > Environment > isi PUBLIC_BASE_URL = URL tadi > Save (redeploy otomatis).
8. Buka `<URL>/api/health` harus `ok`; buka `<URL>/admin`, masuk dengan ADMIN_TOKEN, masukkan ulang API key Gemini.
Catatan: plan gratis tidur setelah 15 menit idle (bangun ~1 menit); data aman di Neon + B2.
