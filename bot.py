import asyncio
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from config import TELEGRAM_BOT_TOKEN, ALERT_CHECK_INTERVAL
from opensea_api import opensea_api
from database import db

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message when /start is issued."""
    welcome_message = """
🎨 **Selamat datang di NFT Floor Price Tracker Bot!**

Bot ini membantu Anda memantau floor price koleksi NFT dari OpenSea.

**📋 Perintah yang tersedia:**

🔍 `/floor <collection_slug>` 
   Cek floor price koleksi NFT
   Contoh: `/floor boredapeyachtclub`

📌 `/track <collection_slug>`
   Tambah koleksi ke daftar pantauan
   Contoh: `/track azuki`

🗑 `/untrack <collection_slug>`
   Hapus koleksi dari daftar pantauan

📋 `/list`
   Lihat semua koleksi yang dipantau

🔔 `/alert <collection_slug> <price>`
   Set alert ketika floor price di bawah target
   Contoh: `/alert boredapeyachtclub 50`

📊 `/check`
   Cek harga semua koleksi yang dipantau

**💡 Tips:**
- Collection slug bisa ditemukan di URL OpenSea, contoh:
  `opensea.io/collection/boredapeyachtclub`
  slug-nya adalah `boredapeyachtclub`
"""
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)


async def floor_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get floor price for a collection."""
    if not context.args:
        await update.message.reply_text(
            "❌ Mohon masukkan collection slug.\n"
            "Contoh: `/floor boredapeyachtclub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    await update.message.reply_text(f"🔍 Mencari data untuk `{collection_slug}`...", 
                                     parse_mode=ParseMode.MARKDOWN)
    
    # Get stats and info in parallel for faster response
    stats, collection_info = await opensea_api.get_floor_price_fast(collection_slug)
    
    if stats is None:
        await update.message.reply_text("❌ Gagal mengambil data. Silakan coba lagi.")
        return
    
    message = opensea_api.format_floor_price(stats, collection_info)
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add collection to tracked list."""
    if not context.args:
        await update.message.reply_text(
            "❌ Mohon masukkan collection slug.\n"
            "Contoh: `/track boredapeyachtclub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    user_id = update.effective_user.id
    
    # Verify collection exists
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
    
    success = db.add_tracked_collection(user_id, collection_slug)
    
    if success:
        await update.message.reply_text(
            f"✅ Berhasil menambahkan `{collection_slug}` ke daftar pantauan!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ `{collection_slug}` sudah ada di daftar pantauan.",
            parse_mode=ParseMode.MARKDOWN
        )


async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove collection from tracked list."""
    if not context.args:
        await update.message.reply_text(
            "❌ Mohon masukkan collection slug.\n"
            "Contoh: `/untrack boredapeyachtclub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    user_id = update.effective_user.id
    
    success = db.remove_tracked_collection(user_id, collection_slug)
    
    if success:
        await update.message.reply_text(
            f"✅ Berhasil menghapus `{collection_slug}` dari daftar pantauan.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ `{collection_slug}` tidak ditemukan di daftar pantauan.",
            parse_mode=ParseMode.MARKDOWN
        )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked collections."""
    user_id = update.effective_user.id
    collections = db.get_tracked_collections(user_id)
    
    if not collections:
        await update.message.reply_text(
            "📋 Anda belum memantau koleksi apapun.\n"
            "Gunakan `/track <collection_slug>` untuk memulai.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    message = "📋 **Koleksi yang Anda pantau:**\n\n"
    for i, slug in enumerate(collections, 1):
        message += f"{i}. `{slug}`\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check prices for all tracked collections."""
    user_id = update.effective_user.id
    collections = db.get_tracked_collections(user_id)
    
    if not collections:
        await update.message.reply_text(
            "📋 Anda belum memantau koleksi apapun.\n"
            "Gunakan `/track <collection_slug>` untuk memulai.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    await update.message.reply_text("🔍 Mengambil data harga...")
    
    message = "📊 **Floor Price Koleksi Anda:**\n\n"
    
    for slug in collections:
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" not in stats:
            total = stats.get("total", {})
            floor_price = total.get("floor_price", 0)
            symbol = total.get("floor_price_symbol", "ETH")
            message += f"• `{slug}`: **{floor_price:.4f} {symbol}**\n"
        else:
            error = stats.get("error", "Unknown error") if stats else "Failed to fetch"
            message += f"• `{slug}`: ❌ {error}\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set price alert for a collection."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: `/alert <collection_slug> <target_price>`\n"
            "Contoh: `/alert boredapeyachtclub 50`\n\n"
            "Anda akan mendapat notifikasi ketika floor price di bawah target.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    
    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Harga target harus berupa angka.")
        return
    
    user_id = update.effective_user.id
    
    # Verify collection exists
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
    
    success = db.add_price_alert(user_id, collection_slug, target_price)
    
    if success:
        await update.message.reply_text(
            f"🔔 Alert berhasil diset!\n\n"
            f"Koleksi: `{collection_slug}`\n"
            f"Target: di bawah **{target_price} ETH**\n\n"
            f"Anda akan mendapat notifikasi ketika floor price turun di bawah target.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Alert untuk `{collection_slug}` dengan target {target_price} ETH sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active alerts for user."""
    user_id = update.effective_user.id
    alerts = db.get_user_alerts(user_id)
    
    if not alerts:
        await update.message.reply_text(
            "🔔 Anda belum memiliki alert aktif.\n"
            "Gunakan `/alert <collection> <price>` untuk membuat alert.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    message = "🔔 **Alert Aktif Anda:**\n\n"
    for slug, price, alert_type in alerts:
        message += f"• `{slug}`: {alert_type} **{price} ETH**\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check price alerts."""
    alerts = db.get_all_active_alerts()
    
    for user_id, collection_slug, target_price, alert_type in alerts:
        try:
            stats = await opensea_api.get_collection_stats(collection_slug)
            if stats and "error" not in stats:
                total = stats.get("total", {})
                current_price = total.get("floor_price", 0)
                symbol = total.get("floor_price_symbol", "ETH")
                
                should_trigger = False
                if alert_type == "below" and current_price < target_price:
                    should_trigger = True
                elif alert_type == "above" and current_price > target_price:
                    should_trigger = True
                
                if should_trigger:
                    message = (
                        f"🚨 **Alert Triggered!**\n\n"
                        f"Koleksi: `{collection_slug}`\n"
                        f"Floor Price: **{current_price:.4f} {symbol}**\n"
                        f"Target: {alert_type} {target_price} {symbol}"
                    )
                    
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=message,
                            parse_mode=ParseMode.MARKDOWN
                        )
                        db.deactivate_alert(user_id, collection_slug, target_price)
                    except Exception as e:
                        logger.error(f"Failed to send alert to user {user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error checking alert for {collection_slug}: {e}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN tidak ditemukan!")
        print("Silakan copy .env.example ke .env dan isi dengan token bot Anda.")
        return
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("floor", floor_command))
    application.add_handler(CommandHandler("track", track_command))
    application.add_handler(CommandHandler("untrack", untrack_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    
    # Add background job for checking alerts
    job_queue = application.job_queue
    job_queue.run_repeating(check_alerts, interval=ALERT_CHECK_INTERVAL, first=60)
    
    # Start the bot
    print("🚀 Bot started! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
