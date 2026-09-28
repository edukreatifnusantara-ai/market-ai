#!/usr/bin/env bash
# ============================================================
# Marketing OS — instalasi lengkap dalam satu jalur:
# venv -> pip install -> pytest -> demo -> git push
# Jalankan:  bash setup.sh
# ============================================================
set -e
cd "$(dirname "$0")"

echo "==> [1/5] Membuat virtual environment .venv ..."
python3 -m venv .venv

echo "==> [2/5] Menginstal dependensi (butuh internet, ±1-2 menit) ..."
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e ".[dev]"

echo "==> [3/5] Menjalankan tes (guardrails, antrean, pipeline end-to-end) ..."
./.venv/bin/pytest -q

echo "==> [4/5] Demo kampanye lengkap (mode mock — aman, tak ada pesan nyata keluar) ..."
./.venv/bin/mos demo

echo "==> [5/5] Commit & push ke GitHub ..."
[ -d .git ] || git init -b main
git add -A
if ! git diff --cached --quiet; then
  git commit -m "Marketing OS: sistem 10 agen AI marketing otonom 24/7

Riset pasar nyata -> strategi -> produksi produk digital -> konten ->
email/WA follow-up -> Sentinel self-healing. Guardrails wajib:
rate limit, opt-in WA, unsubscribe, quiet hours, kill switch.

Co-Authored-By: Claude Code <noreply@anthropic.com>"
fi
git remote get-url origin >/dev/null 2>&1 || \
  git remote add origin https://github.com/edukreatifnusantara-ai/market-ai.git
git push -u origin main || { git pull --rebase origin main && git push -u origin main; }

echo ""
echo "============================================================"
echo " SELESAI. Selanjutnya:"
echo "   1. Isi kredensial di .env (ANTHROPIC_API_KEY, SMTP, WA)"
echo "   2. ./.venv/bin/mos doctor    <- cek semua integrasi hijau"
echo "   3. ./.venv/bin/mos run       <- sistem hidup 24/7"
echo "============================================================"
