# 🎨 NFT Floor Price Tracker Bot

Bot Telegram untuk memantau NFT, portfolio, dan gas Ethereum.

## 🚀 Deploy ke Koyeb Web Service

Gunakan **Web Service** dengan scale **1 instance** selama bot masih memakai long polling dan job scheduler internal. Jika scale lebih dari 1, Telegram polling dan background alert checker bisa berjalan dobel.

### Step 1: Push ke GitHub

```bash
git add .
git commit -m "Add portfolio, volume, gas features"
git push origin main
```

### Step 2: Environment Variables

Tambahkan di Koyeb **"Environment variables"**:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `OPENSEA_API_KEY` | API key OpenSea (opsional) |
| `ETHERSCAN_API_KEY` | API key Etherscan (untuk gas) |
| `DATABASE_URL` | PostgreSQL URL untuk deploy publik multi-user |
| `DATABASE_FILE` | Lokasi SQLite DB fallback, default `nft_tracker.db` |
| `VOLUME_ALERT_COOLDOWN_SECONDS` | Cooldown volume alert, default `21600` |

> Untuk publik multi-user, isi `DATABASE_URL` dari Koyeb Database/managed Postgres. Jika `DATABASE_URL` kosong, bot tetap memakai SQLite lokal dari `DATABASE_FILE`.

## 📋 Commands

### Floor Price & Tracking
| Command | Description |
|---------|-------------|
| `/floor <slug>` | Cek floor price koleksi |
| `/track <slug>` | Track collection |
| `/untrack <slug>` | Untrack collection |
| `/list` | Lihat semua tracked |
| `/check` | Check all tracked prices |

### Alerts
| Command | Description |
|---------|-------------|
| `/alert <slug> <price>` | Alert harga di bawah target |
| `/palert <slug> <persen> [up/down/both]` | Alert perubahan % |
| `/valert <slug> [multiplier]` | Alert volume spike |
| `/gasalert <gwei> [below/above]` | Alert gas price |
| `/alerts` | Lihat semua alert aktif |

### Portfolio
| Command | Description |
|---------|-------------|
| `/addnft <slug> <qty> <buy_price>` | Tambah NFT ke portfolio |
| `/removenft <slug>` | Hapus dari portfolio |
| `/portfolio` | Lihat P/L dan ROI |

### Gas & Volume
| Command | Description |
|---------|-------------|
| `/gas` | Cek harga gas saat ini |
| `/volume <slug>` | Cek volume 24h |

## 🏃 Run Locally

```bash
pip3 install -r requirements.txt
cp .env.example .env
# Edit .env dengan token Anda
python3 bot.py
```
