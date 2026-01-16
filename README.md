# 🎨 NFT Floor Price Tracker Bot

Bot Telegram untuk memantau floor price koleksi NFT dari OpenSea secara real-time.

## ✨ Fitur

- 🔍 **Cek Floor Price** - Lihat floor price koleksi NFT
- 📌 **Track Koleksi** - Pantau beberapa koleksi sekaligus
- 🔔 **Price Alerts** - Dapatkan notifikasi ketika harga mencapai target
- 📊 **Batch Check** - Cek harga semua koleksi yang dipantau

## 🚀 Cara Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Buat Bot Telegram

1. Buka Telegram dan cari [@BotFather](https://t.me/botfather)
2. Kirim `/newbot` dan ikuti instruksi
3. Simpan token bot yang diberikan

### 3. Konfigurasi Environment

```bash
# Copy template file
cp .env.example .env

# Edit .env dan masukkan token bot
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

### 4. (Opsional) OpenSea API Key

Untuk rate limit yang lebih tinggi, dapatkan API key dari [OpenSea Developer Portal](https://docs.opensea.io/reference/api-keys) dan tambahkan ke `.env`:

```
OPENSEA_API_KEY=your_api_key_here
```

### 5. Jalankan Bot

```bash
python bot.py
```

## 📋 Perintah Bot

| Perintah | Deskripsi | Contoh |
|----------|-----------|--------|
| `/start` | Tampilkan pesan selamat datang | `/start` |
| `/floor <slug>` | Cek floor price koleksi | `/floor boredapeyachtclub` |
| `/track <slug>` | Tambah ke daftar pantauan | `/track azuki` |
| `/untrack <slug>` | Hapus dari pantauan | `/untrack azuki` |
| `/list` | Lihat koleksi yang dipantau | `/list` |
| `/check` | Cek harga semua koleksi | `/check` |
| `/alert <slug> <price>` | Set price alert | `/alert boredapeyachtclub 50` |
| `/alerts` | Lihat alert aktif | `/alerts` |

## 💡 Cara Menemukan Collection Slug

Collection slug adalah bagian dari URL OpenSea. Contoh:
- URL: `https://opensea.io/collection/boredapeyachtclub`
- Slug: `boredapeyachtclub`

## 📁 Struktur Project

```
bot-tracker-price-nft/
├── bot.py           # Main bot application
├── opensea_api.py   # OpenSea API integration
├── database.py      # SQLite database handler
├── config.py        # Configuration settings
├── requirements.txt # Python dependencies
├── .env.example     # Environment template
└── README.md        # This file
```

## ⚠️ Catatan Penting

- OpenSea API memiliki rate limit. Jika terlalu banyak request, Anda mungkin perlu menunggu beberapa saat.
- Price alerts dicek secara otomatis setiap 5 menit.
- Data tracking disimpan di SQLite database lokal (`nft_tracker.db`).

## 📜 License

MIT License
