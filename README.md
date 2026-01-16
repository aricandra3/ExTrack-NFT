# 🎨 NFT Floor Price Tracker Bot

Bot Telegram untuk memantau floor price koleksi NFT dari OpenSea.

## 🚀 Deploy ke Koyeb (GRATIS)

### Step 1: Push ke GitHub

```bash
cd "/Users/exsild/Documents/Ngoding/bot/bot tracker price nft"

git init
git add .
git commit -m "NFT floor price bot"

# Buat repo di GitHub, lalu:
git remote add origin https://github.com/USERNAME/nft-bot.git
git push -u origin main
```

### Step 2: Setup Koyeb

1. Buka [koyeb.com](https://www.koyeb.com) → Sign up (gratis)
2. Klik **"Create App"**
3. Pilih **"GitHub"** sebagai deployment method
4. Connect dan pilih repo **nft-bot**
5. Configure:
   - **Builder**: Dockerfile
   - **Instance type**: Free (nano)
   - **Regions**: Pilih yang terdekat

### Step 3: Environment Variables

Tambahkan di bagian **"Environment variables"**:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `OPENSEA_API_KEY` | API key OpenSea |

### Step 4: Deploy

1. Klik **"Deploy"**
2. Tunggu build (~2-3 menit)
3. Bot running 24/7! ✅

## 📋 Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/floor <slug>` | Cek floor price |
| `/track <slug>` | Track collection |
| `/check` | Check all tracked |
| `/alert <slug> <price>` | Set price alert |

## 🏃 Run Locally

```bash
pip3 install -r requirements.txt
cp .env.example .env
# Edit .env dengan token Anda
python3 bot.py
```
