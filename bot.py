import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from config import TELEGRAM_BOT_TOKEN, ALERT_CHECK_INTERVAL, PRICE_HISTORY_INTERVAL, VOLUME_SPIKE_MULTIPLIER
from opensea_api import opensea_api
from gas_api import gas_api
from price_api import price_api
from database import db


class HealthCheckHandler(BaseHTTPRequestHandler):
    def _send_ok(self, method):
        content = b'OK - NFT Floor Price Bot is running'
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if method == 'GET':
            self.wfile.write(content)

    def do_GET(self):
        self._send_ok('GET')
        
    def do_HEAD(self):
        self._send_ok('HEAD')
    
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_health_server():
    """Run health check server in background thread"""
    port = int(os.environ.get('PORT', 8000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"🏥 Health check server running on port {port}")
    server.serve_forever()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ============== Inline Keyboard Menus ==============

def main_menu_keyboard():
    """Main menu with category buttons."""
    keyboard = [
        [InlineKeyboardButton("📊 Price & Tracking", callback_data="menu_price"),
         InlineKeyboardButton("🔔 Alerts", callback_data="menu_alerts")],
        [InlineKeyboardButton("💼 Portofolio", callback_data="menu_portfolio"),
         InlineKeyboardButton("⛽ Gas Fee", callback_data="menu_gas")],
        [InlineKeyboardButton("💱 ETH ↔ IDR", callback_data="menu_converter"),
         InlineKeyboardButton("📖 Bantuan", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def price_menu_keyboard():
    """Sub-menu for price & tracking commands."""
    keyboard = [
        [InlineKeyboardButton("🔍 Floor Price", callback_data="cmd_floor"),
         InlineKeyboardButton("📊 Cek Semua", callback_data="cmd_check")],
        [InlineKeyboardButton("📌 Track", callback_data="cmd_track"),
         InlineKeyboardButton("🗑 Untrack", callback_data="cmd_untrack")],
        [InlineKeyboardButton("📋 List Pantauan", callback_data="cmd_list"),
         InlineKeyboardButton("💎 Volume", callback_data="cmd_volume")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def alerts_menu_keyboard():
    """Sub-menu for alert commands."""
    keyboard = [
        [InlineKeyboardButton("📊 Buat Alert Baru", callback_data="menu_create_alert")],
        [InlineKeyboardButton("🔔 Lihat Semua Alerts", callback_data="cmd_alerts"),
         InlineKeyboardButton("🗑 Hapus Alert", callback_data="cmd_delalert")],
        [InlineKeyboardButton("🗓 Mint Reminder", callback_data="cmd_addmint"),
         InlineKeyboardButton("📋 Lihat Mints", callback_data="cmd_mints")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def create_alert_menu_keyboard():
    """Sub-menu for creating different types of alerts."""
    keyboard = [
        [InlineKeyboardButton("📉 Floor < Target", callback_data="cmd_alert_below"),
         InlineKeyboardButton("📈 Floor > Target", callback_data="cmd_alert_above")],
        [InlineKeyboardButton("📊 % Perubahan", callback_data="cmd_palert"),
         InlineKeyboardButton("💎 Volume Spike", callback_data="cmd_valert")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts")],
    ]
    return InlineKeyboardMarkup(keyboard)


def portfolio_menu_keyboard():
    """Sub-menu for portfolio commands."""
    keyboard = [
        [InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft"),
         InlineKeyboardButton("➖ Hapus NFT", callback_data="cmd_removenft")],
        [InlineKeyboardButton("💰 Lihat Portofolio", callback_data="cmd_portfolio")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def gas_menu_keyboard():
    """Sub-menu for gas commands."""
    keyboard = [
        [InlineKeyboardButton("🔥 Cek Gas", callback_data="cmd_gas"),
         InlineKeyboardButton("⏰ Gas Alert", callback_data="cmd_gasalert")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def converter_menu_keyboard():
    """Sub-menu for ETH price converter."""
    keyboard = [
        [InlineKeyboardButton("💰 Harga ETH", callback_data="cmd_ethprice"),
         InlineKeyboardButton("🔄 Konversi ETH", callback_data="cmd_convert")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============== Menu Text Content ==============

MAIN_MENU_TEXT = (
    "🎨 *NFT Floor Price Tracker*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Selamat datang! Bot ini membantu Anda "
    "memantau harga NFT dan gas Ethereum secara real-time.\n\n"
    "Pilih kategori di bawah untuk memulai 👇"
)

PRICE_MENU_TEXT = (
    "📊 *Price & Tracking*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Pantau floor price koleksi NFT favoritmu.\n\n"
    "Pilih perintah di bawah 👇"
)

ALERTS_MENU_TEXT = (
    "🔔 *Alert Center*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Kelola alert NFT Anda seperti di CoinGecko.\n"
    "Buat alert baru, lihat semua alert aktif, atau hapus yang tidak diperlukan.\n\n"
    "Pilih aksi di bawah 👇"
)

CREATE_ALERT_MENU_TEXT = (
    "📊 *Buat Alert Baru*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "📉 *Floor < Target* — Alert saat floor di bawah harga target\n"
    "📈 *Floor > Target* — Alert saat floor di atas harga target\n"
    "📊 *% Perubahan* — Alert saat floor berubah X% dari harga saat ini\n"
    "💎 *Volume Spike* — Alert saat volume melonjak\n\n"
    "Pilih tipe alert di bawah 👇"
)

PORTFOLIO_MENU_TEXT = (
    "💼 *Portofolio*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Kelola koleksi NFT Anda dan pantau P/L & ROI.\n\n"
    "Pilih perintah di bawah 👇"
)

GAS_MENU_TEXT = (
    "⛽ *Gas Fee*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Monitor harga gas Ethereum dan atur alert.\n\n"
    "Pilih perintah di bawah 👇"
)

CONVERTER_MENU_TEXT = (
    "💱 *ETH ↔ IDR Converter*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Cek harga ETH real-time dan konversi ke Rupiah.\n\n"
    "Pilih perintah di bawah 👇"
)

HELP_TEXT = (
    "📖 *Bantuan*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Cara Pakai:*\n"
    "1️⃣ Tap tombol kategori di bawah\n"
    "2️⃣ Pilih perintah yang ingin digunakan\n"
    "3️⃣ Ikuti format yang ditampilkan\n\n"
    "*Tips:*\n"
    "• Collection slug ada di URL OpenSea\n"
    "  Contoh: `opensea.io/collection/boredapeyachtclub`\n"
    "• Slug = `boredapeyachtclub`\n\n"
    "Ketik /start untuk kembali ke menu utama."
)

# Prompts shown when asking for user input
INPUT_PROMPTS = {
    "floor": "🔍 *Floor Price*\n\nKetik slug koleksi NFT:\n\n_Contoh:_ `boredapeyachtclub`",
    "track": "📌 *Track Koleksi*\n\nKetik slug koleksi yang ingin dipantau:\n\n_Contoh:_ `azuki`",
    "untrack": "🗑 *Untrack Koleksi*\n\nKetik slug koleksi yang ingin dihapus dari pantauan:\n\n_Contoh:_ `azuki`",
    "volume": "💎 *Volume Stats*\n\nKetik slug koleksi NFT:\n\n_Contoh:_ `boredapeyachtclub`",
    "alert_below": "📉 *Price Alert (Floor < Target)*\n\nKetik slug dan harga target ETH:\n\n_Format:_ `slug harga [repeat]`\n_Contoh:_ `boredapeyachtclub 50`\n_Repeat:_ `boredapeyachtclub 50 repeat`",
    "alert_above": "📈 *Price Alert (Floor > Target)*\n\nKetik slug dan harga target ETH:\n\n_Format:_ `slug harga [repeat]`\n_Contoh:_ `azuki 20`\n_Repeat:_ `azuki 20 repeat`",
    "alert": "⚡ *Price Alert*\n\nKetik slug dan harga target:\n\n_Format:_ `slug harga [above/below] [repeat]`\n_Contoh:_ `boredapeyachtclub 50`\n_Contoh:_ `azuki 20 above repeat`",
    "palert": "📊 *% Change Alert*\n\nKetik slug, persen, dan arah:\n\n_Format:_ `slug persen [up/down/both] [repeat]`\n_Contoh:_ `azuki 10 up`\n_Repeat:_ `azuki 10 up repeat`",
    "valert": "📢 *Volume Alert*\n\nKetik slug dan multiplier:\n\n_Format:_ `slug [multiplier]`\n_Contoh:_ `azuki 3`",
    "addnft": "➕ *Tambah NFT*\n\nKetik slug, jumlah, dan harga beli:\n\n_Format:_ `slug jumlah buy_price`\n_Contoh:_ `azuki 2 15.5`",
    "removenft": "➖ *Hapus NFT*\n\nKetik slug koleksi yang ingin dihapus dari portofolio:\n\n_Contoh:_ `azuki`",
    "gasalert": "⏰ *Gas Alert*\n\nKetik target gwei dan tipe:\n\n_Format:_ `gwei [below/above]`\n_Contoh:_ `25 below`",
    "addmint": "🗓 *Mint Reminder*\n\nKetik info mint NFT:\n\n_Format:_ `nama | harga | YYYY-MM-DD HH:MM | link`\n_Contoh:_ `Azuki Elementals | 0.5 ETH | 2026-03-01 14:00 | https://azuki.com/mint`",
    "removemint": "🗑 *Hapus Mint Reminder*\n\nKetik ID reminder yang ingin dihapus:\n\n_Cek ID di_ `/mints`",
    "convert": "🔄 *Konversi ETH*\n\nKetik jumlah ETH yang ingin dikonversi:\n\n_Contoh:_ `0.5` atau `2.5`",
    "delalert": "🗑 *Hapus Alert*\n\nKetik tipe dan ID alert yang ingin dihapus:\n\n_Format:_ `tipe ID`\n_Tipe:_ `price` / `persen` / `volume` / `gas`\n_Contoh:_ `price 5`\n\n_Cek ID di_ 🔔 Lihat Semua Alerts",
}


# ============== Start & Help Commands ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message with main menu keyboard."""
    await update.message.reply_text(
        MAIN_MENU_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help with main menu keyboard."""
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # Menu navigation
    menu_map = {
        "menu_main": (MAIN_MENU_TEXT, main_menu_keyboard()),
        "menu_price": (PRICE_MENU_TEXT, price_menu_keyboard()),
        "menu_alerts": (ALERTS_MENU_TEXT, alerts_menu_keyboard()),
        "menu_create_alert": (CREATE_ALERT_MENU_TEXT, create_alert_menu_keyboard()),
        "menu_portfolio": (PORTFOLIO_MENU_TEXT, portfolio_menu_keyboard()),
        "menu_gas": (GAS_MENU_TEXT, gas_menu_keyboard()),
        "menu_converter": (CONVERTER_MENU_TEXT, converter_menu_keyboard()),
        "menu_help": (HELP_TEXT, main_menu_keyboard()),
    }

    if data in menu_map:
        # Clear any pending action when navigating menus
        context.user_data.pop("pending_action", None)
        text, keyboard = menu_map[data]
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        return

    # ---- No-arg commands: execute directly ----
    if data == "cmd_list":
        collections = db.get_tracked_collections(user_id)
        if not collections:
            text = "📋 Anda belum memantau koleksi apapun.\nGunakan 📌 Track untuk memulai."
        else:
            text = "📋 *Koleksi yang Anda pantau:*\n\n"
            for i, slug in enumerate(collections, 1):
                text += f"{i}. `{slug}`\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📌 Track Baru", callback_data="cmd_track"),
             InlineKeyboardButton("📊 Cek Harga", callback_data="cmd_check")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_price"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_check":
        collections = db.get_tracked_collections(user_id)
        if not collections:
            text = "📋 Anda belum memantau koleksi apapun.\nGunakan 📌 Track untuk memulai."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Track", callback_data="cmd_track"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_price")]
            ])
            await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            return
        await query.edit_message_text("🔍 Mengambil data harga...")
        eth_data = await price_api.get_eth_price()
        idr_rate = eth_data.get("idr", 0) if eth_data and "error" not in eth_data else 0
        text = "📊 *Floor Price Koleksi Anda:*\n\n"
        for slug in collections:
            stats = await opensea_api.get_collection_stats(slug)
            if stats and "error" not in stats:
                total = stats.get("total", {})
                floor_price = total.get("floor_price", 0)
                symbol = total.get("floor_price_symbol", "ETH")
                idr_str = f" (~Rp {floor_price * idr_rate:,.0f})" if idr_rate > 0 else ""
                text += f"• `{slug}`: *{floor_price:.4f} {symbol}*{idr_str}\n"
            else:
                error = stats.get("error", "Unknown error") if stats else "Failed to fetch"
                text += f"• `{slug}`: ❌ {error}\n"
        if idr_rate > 0:
            text += f"\n_💱 Rate: 1 ETH = Rp {idr_rate:,.0f}_"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_check"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_alerts":
        price_alerts = db.get_user_alerts(user_id)
        percent_alerts = db.get_percentage_alerts(user_id)
        volume_alerts = db.get_volume_alerts(user_id)
        gas_alerts_list = db.get_gas_alerts(user_id)
        if not price_alerts and not percent_alerts and not volume_alerts and not gas_alerts_list:
            text = "🔔 Anda belum memiliki alert aktif."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Buat Alert", callback_data="menu_create_alert"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts")]
            ])
        else:
            total_count = len(price_alerts) + len(percent_alerts) + len(volume_alerts) + len(gas_alerts_list)
            text = f"🔔 *Alert Aktif ({total_count})*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            if price_alerts:
                text += "*💰 Price Alerts:*\n"
                for aid, slug, price, alert_type, recurring in price_alerts:
                    direction = "📉 <" if alert_type == "below" else "📈 >"
                    repeat_badge = " 🔁" if recurring else ""
                    text += f"  `#{aid}` `{slug}` {direction} *{price} ETH*{repeat_badge}\n"
                text += "\n"
            if percent_alerts:
                text += "*📊 % Change Alerts:*\n"
                for aid, slug, percent, direction, recurring in percent_alerts:
                    dir_emoji = "📈" if direction == "up" else "📉" if direction == "down" else "↕️"
                    repeat_badge = " 🔁" if recurring else ""
                    text += f"  `#{aid}` `{slug}` {dir_emoji} *{percent}%* {direction}{repeat_badge}\n"
                text += "\n"
            if volume_alerts:
                text += "*💎 Volume Alerts:*\n"
                for aid, slug, multiplier in volume_alerts:
                    text += f"  `#{aid}` `{slug}` spike *{multiplier}x*\n"
                text += "\n"
            if gas_alerts_list:
                text += "*⛽ Gas Alerts:*\n"
                for aid, gwei, alert_type in gas_alerts_list:
                    text += f"  `#{aid}` {alert_type} *{gwei} gwei*\n"
            text += "\n_🔁 = recurring (alert aktif kembali setelah trigger)_"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Tambah Alert", callback_data="menu_create_alert"),
                 InlineKeyboardButton("🗑 Hapus Alert", callback_data="cmd_delalert")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
            ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_portfolio":
        portfolio = db.get_portfolio(user_id)
        if not portfolio:
            text = "💼 Portofolio Anda kosong."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_portfolio")]
            ])
            await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            return
        await query.edit_message_text("💼 Menghitung portofolio Anda...")
        text = "💼 *Portofolio Anda*\n\n"
        total_cost = 0
        total_value = 0
        for slug, quantity, buy_price, _ in portfolio:
            stats = await opensea_api.get_collection_stats(slug)
            if stats and "error" not in stats:
                total_data = stats.get("total", {})
                current_price = total_data.get("floor_price", 0) or 0
                symbol = total_data.get("floor_price_symbol", "ETH")
                item_cost = quantity * buy_price
                item_value = quantity * current_price
                pl = item_value - item_cost
                roi = ((item_value - item_cost) / item_cost * 100) if item_cost > 0 else 0
                emoji = "🟢" if pl >= 0 else "🔴"
                sign = "+" if pl >= 0 else ""
                text += f"*{slug.upper()}* ({quantity} NFT)\n"
                text += f"├ Buy: {buy_price:.4f} {symbol}\n"
                text += f"├ Now: {current_price:.4f} {symbol}\n"
                text += f"├ P/L: {sign}{pl:.4f} {symbol} ({sign}{roi:.1f}%) {emoji}\n"
                text += f"└ Value: {item_value:.4f} {symbol}\n\n"
                total_cost += item_cost
                total_value += item_value
            else:
                text += f"*{slug.upper()}* ({quantity} NFT)\n"
                text += f"└ ❌ Gagal ambil harga\n\n"
                total_cost += quantity * buy_price
        total_pl = total_value - total_cost
        total_roi = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
        emoji = "🟢" if total_pl >= 0 else "🔴"
        sign = "+" if total_pl >= 0 else ""
        text += "━━━━━━━━━━━━━━━━\n"
        text += f"📊 *TOTAL*\n"
        text += f"├ Cost Basis: *{total_cost:.4f} ETH*\n"
        text += f"├ Current Value: *{total_value:.4f} ETH*\n"
        text += f"├ Unrealized P/L: *{sign}{total_pl:.4f} ETH*\n"
        text += f"└ ROI: *{sign}{total_roi:.1f}%* {emoji}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_portfolio"),
             InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_portfolio"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_gas":
        await query.edit_message_text("⛽ Mengambil data gas...")
        gas_data = await gas_api.get_gas_price()
        text = gas_api.format_gas_price(gas_data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_gas"),
             InlineKeyboardButton("⏰ Set Gas Alert", callback_data="cmd_gasalert")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_gas"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_ethprice":
        await query.edit_message_text("💱 Mengambil harga ETH...")
        eth_data = await price_api.get_eth_price()
        text = price_api.format_eth_price(eth_data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_ethprice"),
             InlineKeyboardButton("🔄 Konversi", callback_data="cmd_convert")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_converter"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_mints":
        reminders = db.get_mint_reminders(user_id)
        if not reminders:
            text = "🗓 Anda belum memiliki mint reminder."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah Reminder", callback_data="cmd_addmint"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts")]
            ])
        else:
            text = "🗓 *Mint Reminders Aktif:*\n\n"
            for rid, name, price, mdate, link in reminders:
                text += f"*#{rid} — {name}*\n"
                text += f"├ 💰 Price: {price}\n"
                text += f"├ 📅 Date: `{mdate}`\n"
                if link:
                    text += f"└ 🔗 [Mint Link]({link})\n"
                else:
                    text += f"└ 🔗 No link\n"
                text += "\n"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah", callback_data="cmd_addmint"),
                 InlineKeyboardButton("🗑 Hapus", callback_data="cmd_removemint")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
            ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
                                      disable_web_page_preview=True)
        return

    # ---- Arg-required commands: prompt for input ----
    if data.startswith("cmd_"):
        action = data[4:]  # e.g. "floor", "track", "alert"
        if action in INPUT_PROMPTS:
            context.user_data["pending_action"] = action
            cancel_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Batal", callback_data="menu_main")]
            ])
            await query.edit_message_text(
                text=INPUT_PROMPTS[action],
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=cancel_kb
            )
            return

    # ---- Quick actions after floor check ----
    if data.startswith("qa_alert_"):
        slug = data[9:]  # extract slug from "qa_alert_<slug>"
        context.user_data["pending_action"] = "alert"
        context.user_data["pending_slug"] = slug
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batal", callback_data="menu_main")]
        ])
        await query.edit_message_text(
            text=f"⚡ *Set Alert untuk* `{slug}`\n\nKetik harga target (ETH):\n\n_Contoh:_ `50`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=cancel_kb
        )
        return

    if data.startswith("qa_track_"):
        slug = data[9:]  # extract slug from "qa_track_<slug>"
        success = db.add_tracked_collection(user_id, slug)
        if success:
            text = f"✅ `{slug}` berhasil ditambahkan ke watchlist!"
        else:
            text = f"ℹ️ `{slug}` sudah ada di watchlist Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Watchlist", callback_data="cmd_list"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return


async def pending_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input for pending actions from menu buttons."""
    action = context.user_data.pop("pending_action", None)
    if not action:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    pending_slug = context.user_data.pop("pending_slug", None)

    # ---- ETH Converter ----
    if action == "convert":
        try:
            eth_amount = float(text.split()[0])
        except (ValueError, IndexError):
            await update.message.reply_text(
                "❌ Masukkan jumlah ETH berupa angka.\n_Contoh:_ `0.5`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        eth_data = await price_api.get_eth_price()
        if eth_data is None or "error" in eth_data:
            error_msg = eth_data.get("error", "Gagal mengambil data") if eth_data else "Gagal mengambil data"
            await update.message.reply_text(f"❌ {error_msg}")
            return
        usd_rate = eth_data.get("usd", 0)
        idr_rate = eth_data.get("idr", 0)
        usd_value = eth_amount * usd_rate
        idr_value = eth_amount * idr_rate
        msg = (
            f"💱 *Konversi ETH*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔢 *Jumlah:* `{eth_amount} ETH`\n\n"
            f"🇺🇸 *USD:*  `${usd_value:,.2f}`\n"
            f"🇮🇩 *IDR:*  `Rp {idr_value:,.0f}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 _Rate: 1 ETH = ${usd_rate:,.2f} / Rp {idr_rate:,.0f}_"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Konversi Lagi", callback_data="cmd_convert"),
             InlineKeyboardButton("💰 Harga ETH", callback_data="cmd_ethprice")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    # ---- Single-arg commands ----
    if action == "floor":
        slug = text.split()[0].lower()
        await update.message.reply_text(
            f"🔍 Mencari data untuk `{slug}`...",
            parse_mode=ParseMode.MARKDOWN
        )
        stats, collection_info = await opensea_api.get_floor_price_fast(slug)
        if stats is None:
            await update.message.reply_text("❌ Gagal mengambil data. Silakan coba lagi.")
            return
        message = opensea_api.format_floor_price(stats, collection_info)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Set Alert", callback_data=f"qa_alert_{slug}"),
             InlineKeyboardButton("📌 Add Watchlist", callback_data=f"qa_track_{slug}")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "track":
        slug = text.split()[0].lower()
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        success = db.add_tracked_collection(user_id, slug)
        if success:
            msg = f"✅ `{slug}` berhasil ditambahkan ke watchlist!"
        else:
            msg = f"ℹ️ `{slug}` sudah ada di watchlist Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Watchlist", callback_data="cmd_list"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "untrack":
        slug = text.split()[0].lower()
        success = db.remove_tracked_collection(user_id, slug)
        if success:
            msg = f"✅ `{slug}` berhasil dihapus dari watchlist."
        else:
            msg = f"❌ `{slug}` tidak ditemukan di watchlist Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Watchlist", callback_data="cmd_list"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "volume":
        slug = text.split()[0].lower()
        await update.message.reply_text(
            f"📊 Mengambil data volume untuk `{slug}`...",
            parse_mode=ParseMode.MARKDOWN
        )
        stats, collection_info = await opensea_api.get_floor_price_fast(slug)
        if stats is None:
            await update.message.reply_text("❌ Gagal mengambil data. Silakan coba lagi.")
            return
        previous_volume = db.get_average_volume(slug)
        message = opensea_api.format_volume_stats(stats, collection_info, previous_volume)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Set Volume Alert", callback_data=f"qa_alert_{slug}"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "removenft":
        slug = text.split()[0].lower()
        success = db.remove_portfolio_item(user_id, slug)
        if success:
            msg = f"✅ `{slug}` berhasil dihapus dari portofolio."
        else:
            msg = f"❌ `{slug}` tidak ditemukan di portofolio Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Lihat Portofolio", callback_data="cmd_portfolio"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    # ---- Multi-arg commands ----
    if action in ("alert", "alert_below", "alert_above"):
        parts = text.split()
        if action == "alert_below":
            alert_type = "below"
        elif action == "alert_above":
            alert_type = "above"
        else:
            alert_type = "below"  # default for generic alert

        if pending_slug:
            slug = pending_slug
            try:
                target_price = float(parts[0])
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Masukkan harga target berupa angka.\n_Contoh:_ `50`", parse_mode=ParseMode.MARKDOWN)
                return
            is_recurring = "repeat" in [p.lower() for p in parts[1:]]
        else:
            if len(parts) < 2:
                await update.message.reply_text("❌ Format: `slug harga [repeat]`\n_Contoh:_ `boredapeyachtclub 50`", parse_mode=ParseMode.MARKDOWN)
                return
            slug = parts[0].lower()
            try:
                target_price = float(parts[1])
            except ValueError:
                await update.message.reply_text("❌ Harga target harus berupa angka.")
                return
            # For generic /alert, check for above/below keyword
            if action == "alert":
                for p in parts[2:]:
                    if p.lower() in ("above", "below"):
                        alert_type = p.lower()
                        break
            is_recurring = "repeat" in [p.lower() for p in parts[2:]]

        # Get current price for context
        await update.message.reply_text(f"🔍 Memverifikasi `{slug}`...", parse_mode=ParseMode.MARKDOWN)
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        current_price = 0
        symbol = "ETH"
        if stats:
            total = stats.get("total", {})
            current_price = total.get("floor_price", 0) or 0
            symbol = total.get("floor_price_symbol", "ETH")

        success = db.add_price_alert(user_id, slug, target_price, alert_type, 
                                      is_recurring=is_recurring, current_price=current_price)
        direction_text = "di bawah" if alert_type == "below" else "di atas"
        direction_emoji = "📉" if alert_type == "below" else "📈"
        repeat_text = "\n🔁 _Recurring: alert aktif kembali setelah trigger_" if is_recurring else ""
        if success:
            msg = (f"✅ *Alert Berhasil Dibuat!*\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"📦 Koleksi: `{slug}`\n"
                   f"💰 Floor saat ini: *{current_price:.4f} {symbol}*\n"
                   f"{direction_emoji} Target: {direction_text} *{target_price} {symbol}*"
                   f"{repeat_text}")
        else:
            msg = f"ℹ️ Alert untuk `{slug}` dengan target {target_price} {symbol} ({alert_type}) sudah ada."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Lihat Alerts", callback_data="cmd_alerts"),
             InlineKeyboardButton("📊 Alert Lagi", callback_data="menu_create_alert")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "palert":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Format: `slug persen [up/down/both] [repeat]`\n_Contoh:_ `azuki 10 up`", parse_mode=ParseMode.MARKDOWN)
            return
        slug = parts[0].lower()
        try:
            percentage = float(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Persentase harus berupa angka.")
            return
        direction = "both"
        for p in parts[2:]:
            if p.lower() in ("up", "down", "both"):
                direction = p.lower()
                break
        is_recurring = "repeat" in [p.lower() for p in parts[2:]]

        # Get current price as reference
        await update.message.reply_text(f"🔍 Memverifikasi `{slug}`...", parse_mode=ParseMode.MARKDOWN)
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        ref_price = 0
        symbol = "ETH"
        if stats:
            total = stats.get("total", {})
            ref_price = total.get("floor_price", 0) or 0
            symbol = total.get("floor_price_symbol", "ETH")

        success = db.add_percentage_alert(user_id, slug, percentage, direction,
                                           is_recurring=is_recurring, reference_price=ref_price)
        direction_text = {"up": "📈 naik", "down": "📉 turun", "both": "↕️ naik/turun"}
        repeat_text = "\n🔁 _Recurring: alert aktif kembali setelah trigger_" if is_recurring else ""
        if success:
            msg = (f"✅ *% Alert Berhasil Dibuat!*\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"📦 Koleksi: `{slug}`\n"
                   f"💰 Harga referensi: *{ref_price:.4f} {symbol}*\n"
                   f"{direction_text[direction]} *{percentage}%*"
                   f"{repeat_text}")
        else:
            msg = f"ℹ️ Alert untuk `{slug}` dengan setting ini sudah ada."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Lihat Alerts", callback_data="cmd_alerts"),
             InlineKeyboardButton("📊 Alert Lagi", callback_data="menu_create_alert")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "valert":
        parts = text.split()
        slug = parts[0].lower()
        try:
            multiplier = float(parts[1]) if len(parts) > 1 else VOLUME_SPIKE_MULTIPLIER
        except ValueError:
            multiplier = VOLUME_SPIKE_MULTIPLIER
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        success = db.add_volume_alert(user_id, slug, multiplier)
        if success:
            msg = (f"✅ *Volume Alert Berhasil Dibuat!*\n"
                   f"━━━━━━━━━━━━━━━━━━━━\n\n"
                   f"📦 Koleksi: `{slug}`\n"
                   f"💎 Trigger: Volume *{multiplier}x* dari rata-rata")
        else:
            msg = f"ℹ️ Volume alert untuk `{slug}` sudah ada."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Lihat Alerts", callback_data="cmd_alerts"),
             InlineKeyboardButton("📊 Alert Lagi", callback_data="menu_create_alert")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "delalert":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format: `tipe ID`\n"
                "_Tipe:_ `price` / `persen` / `volume` / `gas`\n"
                "_Contoh:_ `price 5`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        alert_type = parts[0].lower()
        try:
            alert_id = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ ID harus berupa angka.")
            return
        
        type_map = {
            "price": ("Price Alert", db.remove_alert_by_id),
            "persen": ("% Alert", db.remove_percent_alert_by_id),
            "percent": ("% Alert", db.remove_percent_alert_by_id),
            "volume": ("Volume Alert", db.remove_volume_alert_by_id),
            "gas": ("Gas Alert", db.remove_gas_alert_by_id),
        }
        if alert_type not in type_map:
            await update.message.reply_text(
                "❌ Tipe tidak valid. Gunakan: `price` / `persen` / `volume` / `gas`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        type_name, remove_func = type_map[alert_type]
        success = remove_func(user_id, alert_id)
        if success:
            msg = f"✅ {type_name} `#{alert_id}` berhasil dihapus!"
        else:
            msg = f"❌ {type_name} `#{alert_id}` tidak ditemukan atau bukan milik Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Lihat Alerts", callback_data="cmd_alerts"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "addnft":
        parts = text.split()
        if len(parts) < 3:
            await update.message.reply_text("❌ Format: `slug jumlah buy_price`\n_Contoh:_ `azuki 2 15.5`", parse_mode=ParseMode.MARKDOWN)
            return
        slug = parts[0].lower()
        try:
            quantity = int(parts[1])
            buy_price = float(parts[2])
        except ValueError:
            await update.message.reply_text("❌ Jumlah harus integer dan harga harus angka.")
            return
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        success = db.add_portfolio_item(user_id, slug, quantity, buy_price)
        if success:
            total_cost = quantity * buy_price
            msg = (f"✅ NFT berhasil ditambahkan ke portofolio!\n\n"
                   f"Koleksi: `{slug}`\n"
                   f"Jumlah: *{quantity} NFT*\n"
                   f"Buy Price: *{buy_price} ETH* per NFT\n"
                   f"Total Cost: *{total_cost:.4f} ETH*")
        else:
            msg = "❌ Gagal menambahkan ke portofolio."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Lihat Portofolio", callback_data="cmd_portfolio"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "gasalert":
        parts = text.split()
        try:
            target_gwei = float(parts[0])
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Target gwei harus berupa angka.\n_Contoh:_ `25 below`", parse_mode=ParseMode.MARKDOWN)
            return
        alert_type = parts[1].lower() if len(parts) > 1 else "below"
        if alert_type not in ["below", "above"]:
            alert_type = "below"
        success = db.add_gas_alert(user_id, target_gwei, alert_type)
        type_text = "di bawah" if alert_type == "below" else "di atas"
        if success:
            msg = (f"⛽ Gas Alert berhasil diset!\n\n"
                   f"Alert: Gas {type_text} *{target_gwei} gwei*")
        else:
            msg = "ℹ️ Gas alert dengan setting ini sudah ada."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Lihat Alerts", callback_data="cmd_alerts"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "addmint":
        parts = text.split("|")
        if len(parts) < 3:
            await update.message.reply_text(
                "❌ Format salah.\n\n"
                "_Format:_ `nama | harga | YYYY-MM-DD HH:MM | link`\n"
                "_Contoh:_ `Azuki Elementals | 0.5 ETH | 2026-03-01 14:00 | https://azuki.com/mint`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        nft_name = parts[0].strip()
        mint_price = parts[1].strip()
        date_str = parts[2].strip()
        mint_link = parts[3].strip() if len(parts) > 3 else ""
        try:
            mint_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except ValueError:
            await update.message.reply_text(
                "❌ Format tanggal salah.\nGunakan: `YYYY-MM-DD HH:MM`\n_Contoh:_ `2026-03-01 14:00`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        success = db.add_mint_reminder(user_id, nft_name, mint_price, date_str, mint_link)
        if success:
            msg = (f"🗓 Mint Reminder berhasil ditambahkan!\n\n"
                   f"NFT: *{nft_name}*\n"
                   f"Price: {mint_price}\n"
                   f"Date: `{date_str}`\n")
            if mint_link:
                msg += f"Link: {mint_link}\n"
            msg += "\n_Bot akan mengingatkan 30 menit & 5 menit sebelum mint._"
        else:
            msg = "❌ Gagal menambahkan reminder."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Reminders", callback_data="cmd_mints"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
                                        disable_web_page_preview=True)
        return

    if action == "removemint":
        try:
            reminder_id = int(text.strip())
        except ValueError:
            await update.message.reply_text("❌ ID harus berupa angka. Cek ID di /mints")
            return
        success = db.remove_mint_reminder(user_id, reminder_id)
        if success:
            msg = f"✅ Reminder #{reminder_id} berhasil dihapus."
        else:
            msg = f"❌ Reminder #{reminder_id} tidak ditemukan."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Reminders", callback_data="cmd_mints"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return


async def post_init(application: Application) -> None:
    """Set bot commands for Telegram's built-in menu."""
    commands = [
        BotCommand("start", "🏠 Menu utama"),
        BotCommand("help", "📖 Bantuan"),
        BotCommand("floor", "🔍 Cek floor price"),
        BotCommand("track", "📌 Pantau koleksi"),
        BotCommand("untrack", "🗑 Hapus pantauan"),
        BotCommand("list", "📋 Daftar pantauan"),
        BotCommand("check", "📊 Cek semua harga"),
        BotCommand("alert", "⚡ Set price alert"),
        BotCommand("palert", "📈 Set % alert"),
        BotCommand("valert", "📢 Set volume alert"),
        BotCommand("alerts", "🔔 Lihat semua alert"),
        BotCommand("addnft", "➕ Tambah NFT"),
        BotCommand("removenft", "➖ Hapus NFT"),
        BotCommand("portfolio", "💼 Lihat portofolio"),
        BotCommand("gas", "⛽ Cek gas price"),
        BotCommand("gasalert", "⏰ Set gas alert"),
        BotCommand("ethprice", "💱 Harga ETH real-time"),
        BotCommand("convert", "🔄 Konversi ETH ke IDR"),
        BotCommand("addmint", "🗓 Tambah mint reminder"),
        BotCommand("mints", "📋 Lihat mint reminders"),
        BotCommand("removemint", "🗑 Hapus mint reminder"),
    ]
    await application.bot.set_my_commands(commands)


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
    # Quick action buttons after floor check
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Set Alert", callback_data=f"qa_alert_{collection_slug}"),
         InlineKeyboardButton("📌 Add Watchlist", callback_data=f"qa_track_{collection_slug}")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


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
            "❌ Format: `/alert <slug> <harga> [above/below] [repeat]`\n"
            "Contoh: `/alert boredapeyachtclub 50`\n"
            "        `/alert azuki 15 above repeat`\n\n"
            "Default: below (di bawah harga).",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    
    try:
        target_price = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Harga target harus berupa angka.")
        return
    
    alert_type = "below"
    for arg in context.args[2:]:
        if arg.lower() in ("above", "below"):
            alert_type = arg.lower()
            break
            
    is_recurring = "repeat" in [a.lower() for a in context.args[2:]]
    
    user_id = update.effective_user.id
    
    # Verify collection exists and get current price
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
        
    current_price = 0
    symbol = "ETH"
    if stats:
        total = stats.get("total", {})
        current_price = total.get("floor_price", 0) or 0
        symbol = total.get("floor_price_symbol", "ETH")
    
    success = db.add_price_alert(user_id, collection_slug, target_price, alert_type,
                                 is_recurring=is_recurring, current_price=current_price)
    
    direction_text = "di bawah" if alert_type == "below" else "di atas"
    direction_emoji = "📉" if alert_type == "below" else "📈"
    repeat_text = "\n🔁 _Recurring: alert aktif kembali setelah trigger_" if is_recurring else ""
    
    if success:
        await update.message.reply_text(
            f"✅ *Alert Berhasil Dibuat!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Koleksi: `{collection_slug}`\n"
            f"💰 Floor saat ini: *{current_price:.4f} {symbol}*\n"
            f"{direction_emoji} Target: {direction_text} *{target_price} {symbol}*"
            f"{repeat_text}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Alert untuk `{collection_slug}` dengan target {target_price} {symbol} ({alert_type}) sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active alerts for user."""
    user_id = update.effective_user.id
    
    # Get all types of alerts
    price_alerts = db.get_user_alerts(user_id)
    percent_alerts = db.get_percentage_alerts(user_id)
    volume_alerts = db.get_volume_alerts(user_id)
    gas_alerts = db.get_gas_alerts(user_id)
    
    if not price_alerts and not percent_alerts and not volume_alerts and not gas_alerts:
        await update.message.reply_text(
            "🔔 Anda belum memiliki alert aktif.\n"
            "Gunakan `/alert`, `/palert`, `/valert`, atau `/gasalert` untuk membuat alert.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    message = f"🔔 *Alert Aktif ({len(price_alerts) + len(percent_alerts) + len(volume_alerts) + len(gas_alerts)})*\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if price_alerts:
        message += "*💰 Price Alerts:*\n"
        for aid, slug, price, alert_type, recurring in price_alerts:
            direction = "📉 <" if alert_type == "below" else "📈 >"
            repeat_badge = " 🔁" if recurring else ""
            message += f"  `#{aid}` `{slug}` {direction} *{price} ETH*{repeat_badge}\n"
        message += "\n"
    
    if percent_alerts:
        message += "*📊 % Change Alerts:*\n"
        for aid, slug, percent, direction, recurring in percent_alerts:
            dir_emoji = "📈" if direction == "up" else "📉" if direction == "down" else "↕️"
            repeat_badge = " 🔁" if recurring else ""
            message += f"  `#{aid}` `{slug}` {dir_emoji} *{percent}%* {direction}{repeat_badge}\n"
        message += "\n"
    
    if volume_alerts:
        message += "*💎 Volume Alerts:*\n"
        for aid, slug, multiplier in volume_alerts:
            message += f"  `#{aid}` `{slug}` spike *{multiplier}x*\n"
        message += "\n"
    
    if gas_alerts:
        message += "*⛽ Gas Alerts:*\n"
        for aid, gwei, alert_type in gas_alerts:
            message += f"  `#{aid}` {alert_type} *{gwei} gwei*\n"
            
    message += "\n_🔁 = recurring (alert aktif kembali setelah trigger)_\n\n"
    message += "_Gunakan_ `/delalert <tipe> <id>` _untuk menghapus._"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def delalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete an alert by ID."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: `/delalert <tipe> <id>`\n"
            "_Tipe:_ `price` / `persen` / `volume` / `gas`\n"
            "_Contoh:_ `/delalert price 5`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    alert_type = context.args[0].lower()
    try:
        alert_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ ID harus berupa angka.")
        return
        
    user_id = update.effective_user.id
    type_map = {
        "price": ("Price Alert", db.remove_alert_by_id),
        "persen": ("% Alert", db.remove_percent_alert_by_id),
        "percent": ("% Alert", db.remove_percent_alert_by_id),
        "volume": ("Volume Alert", db.remove_volume_alert_by_id),
        "gas": ("Gas Alert", db.remove_gas_alert_by_id),
    }
    
    if alert_type not in type_map:
        await update.message.reply_text(
            "❌ Tipe tidak valid. Gunakan: `price` / `persen` / `volume` / `gas`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    type_name, remove_func = type_map[alert_type]
    success = remove_func(user_id, alert_id)
    if success:
        await update.message.reply_text(f"✅ {type_name} `#{alert_id}` berhasil dihapus!", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ {type_name} `#{alert_id}` tidak ditemukan atau bukan milik Anda.", parse_mode=ParseMode.MARKDOWN)


# ============== Percentage Alert Commands ==============

async def palert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set percentage-based price alert."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: `/palert <slug> <persen> [up/down/both] [repeat]`\n\n"
            "Contoh:\n"
            "• `/palert azuki 10 up` - Alert naik 10%\n"
            "• `/palert azuki 15 both repeat` - Alert naik/turun 15% berulang",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    
    try:
        percentage = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Persentase harus berupa angka.")
        return
    
    direction = "both"
    for p in context.args[2:]:
        if p.lower() in ("up", "down", "both"):
            direction = p.lower()
            break
            
    is_recurring = "repeat" in [p.lower() for p in context.args[2:]]
    
    user_id = update.effective_user.id
    
    # Verify collection exists and get ref price
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
        
    ref_price = 0
    symbol = "ETH"
    if stats:
        total = stats.get("total", {})
        ref_price = total.get("floor_price", 0) or 0
        symbol = total.get("floor_price_symbol", "ETH")
    
    success = db.add_percentage_alert(user_id, collection_slug, percentage, direction,
                                      is_recurring=is_recurring, reference_price=ref_price)
    
    direction_text = {"up": "📈 naik", "down": "📉 turun", "both": "↕️ naik/turun"}
    repeat_text = "\n🔁 _Recurring: alert aktif kembali setelah trigger_" if is_recurring else ""
    
    if success:
        await update.message.reply_text(
            f"✅ *% Alert Berhasil Dibuat!*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Koleksi: `{collection_slug}`\n"
            f"💰 Harga referensi: *{ref_price:.4f} {symbol}*\n"
            f"{direction_text[direction]} *{percentage}%*"
            f"{repeat_text}",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Alert untuk `{collection_slug}` dengan setting ini sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


# ============== Volume Alert Commands ==============

async def volume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check volume stats for a collection."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/volume <collection_slug>`\n"
            "Contoh: `/volume boredapeyachtclub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    await update.message.reply_text(f"📊 Mengambil data volume untuk `{collection_slug}`...", 
                                     parse_mode=ParseMode.MARKDOWN)
    
    stats, collection_info = await opensea_api.get_floor_price_fast(collection_slug)
    
    if stats is None:
        await update.message.reply_text("❌ Gagal mengambil data. Silakan coba lagi.")
        return
    
    # Get previous volume for comparison
    previous_volume = db.get_average_volume(collection_slug)
    
    message = opensea_api.format_volume_stats(stats, collection_info, previous_volume)
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def valert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set volume spike alert."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/valert <slug> [multiplier]`\n\n"
            "Contoh:\n"
            "• `/valert azuki` - Alert volume spike 2x (default)\n"
            "• `/valert azuki 3` - Alert volume spike 3x",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    
    try:
        multiplier = float(context.args[1]) if len(context.args) > 1 else VOLUME_SPIKE_MULTIPLIER
    except ValueError:
        multiplier = VOLUME_SPIKE_MULTIPLIER
    
    user_id = update.effective_user.id
    
    # Verify collection exists
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
    
    success = db.add_volume_alert(user_id, collection_slug, multiplier)
    
    if success:
        await update.message.reply_text(
            f"📊 Volume Spike Alert berhasil diset!\n\n"
            f"Koleksi: `{collection_slug}`\n"
            f"Trigger: Volume **{multiplier}x** dari rata-rata\n\n"
            f"Bot akan monitor volume 24h dan bandingkan dengan rata-rata 7 hari.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Volume alert untuk `{collection_slug}` sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


# ============== Portfolio Commands ==============

async def addnft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add NFT to portfolio."""
    if len(context.args) < 3:
        await update.message.reply_text(
            "❌ Format: `/addnft <slug> <jumlah> <buy_price>`\n\n"
            "Contoh: `/addnft azuki 2 15.5`\n"
            "(Punya 2 Azuki, beli di 15.5 ETH per NFT)",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    
    try:
        quantity = int(context.args[1])
        buy_price = float(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ Jumlah harus integer dan harga harus angka.")
        return
    
    user_id = update.effective_user.id
    
    # Verify collection exists
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return
    
    success = db.add_portfolio_item(user_id, collection_slug, quantity, buy_price)
    
    if success:
        total_cost = quantity * buy_price
        await update.message.reply_text(
            f"✅ NFT berhasil ditambahkan ke portofolio!\n\n"
            f"Koleksi: `{collection_slug}`\n"
            f"Jumlah: **{quantity} NFT**\n"
            f"Buy Price: **{buy_price} ETH** per NFT\n"
            f"Total Cost: **{total_cost:.4f} ETH**",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("❌ Gagal menambahkan ke portofolio.")


async def removenft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove NFT from portfolio."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/removenft <slug>`\n"
            "Contoh: `/removenft azuki`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    collection_slug = context.args[0].lower()
    user_id = update.effective_user.id
    
    success = db.remove_portfolio_item(user_id, collection_slug)
    
    if success:
        await update.message.reply_text(
            f"✅ `{collection_slug}` berhasil dihapus dari portofolio.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ `{collection_slug}` tidak ditemukan di portofolio Anda.",
            parse_mode=ParseMode.MARKDOWN
        )


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user portfolio with P/L and ROI."""
    user_id = update.effective_user.id
    portfolio = db.get_portfolio(user_id)
    
    if not portfolio:
        await update.message.reply_text(
            "💼 Portofolio Anda kosong.\n"
            "Gunakan `/addnft <slug> <jumlah> <buy_price>` untuk menambahkan.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    await update.message.reply_text("💼 Menghitung portofolio Anda...")
    
    message = "💼 **Portofolio Anda**\n\n"
    total_cost = 0
    total_value = 0
    
    for slug, quantity, buy_price, _ in portfolio:
        # Get current floor price
        stats = await opensea_api.get_collection_stats(slug)
        
        if stats and "error" not in stats:
            total_data = stats.get("total", {})
            current_price = total_data.get("floor_price", 0) or 0
            symbol = total_data.get("floor_price_symbol", "ETH")
            
            item_cost = quantity * buy_price
            item_value = quantity * current_price
            pl = item_value - item_cost
            roi = ((item_value - item_cost) / item_cost * 100) if item_cost > 0 else 0
            
            emoji = "🟢" if pl >= 0 else "🔴"
            sign = "+" if pl >= 0 else ""
            
            message += f"**{slug.upper()}** ({quantity} NFT)\n"
            message += f"├ Buy: {buy_price:.4f} {symbol}\n"
            message += f"├ Now: {current_price:.4f} {symbol}\n"
            message += f"├ P/L: {sign}{pl:.4f} {symbol} ({sign}{roi:.1f}%) {emoji}\n"
            message += f"└ Value: {item_value:.4f} {symbol}\n\n"
            
            total_cost += item_cost
            total_value += item_value
        else:
            message += f"**{slug.upper()}** ({quantity} NFT)\n"
            message += f"└ ❌ Gagal ambil harga\n\n"
            total_cost += quantity * buy_price
    
    # Summary
    total_pl = total_value - total_cost
    total_roi = ((total_value - total_cost) / total_cost * 100) if total_cost > 0 else 0
    emoji = "🟢" if total_pl >= 0 else "🔴"
    sign = "+" if total_pl >= 0 else ""
    
    message += "━━━━━━━━━━━━━━━━\n"
    message += f"📊 **TOTAL**\n"
    message += f"├ Cost Basis: **{total_cost:.4f} ETH**\n"
    message += f"├ Current Value: **{total_value:.4f} ETH**\n"
    message += f"├ Unrealized P/L: **{sign}{total_pl:.4f} ETH**\n"
    message += f"└ ROI: **{sign}{total_roi:.1f}%** {emoji}"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

# ============== ETH Price & Converter Commands ==============

async def ethprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check current ETH price in USD and IDR."""
    await update.message.reply_text("💱 Mengambil harga ETH...")

    eth_data = await price_api.get_eth_price()
    message = price_api.format_eth_price(eth_data)
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert ETH amount to USD and IDR."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/convert <jumlah_eth>`\n"
            "Contoh: `/convert 0.5`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        eth_amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Jumlah ETH harus berupa angka.")
        return

    eth_data = await price_api.get_eth_price()
    if eth_data is None or "error" in eth_data:
        error_msg = eth_data.get("error", "Gagal mengambil data") if eth_data else "Gagal mengambil data"
        await update.message.reply_text(f"❌ {error_msg}")
        return

    usd_rate = eth_data.get("usd", 0)
    idr_rate = eth_data.get("idr", 0)
    usd_value = eth_amount * usd_rate
    idr_value = eth_amount * idr_rate

    message = (
        f"💱 *Konversi ETH*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 *Jumlah:* `{eth_amount} ETH`\n\n"
        f"🇺🇸 *USD:*  `${usd_value:,.2f}`\n"
        f"🇮🇩 *IDR:*  `Rp {idr_value:,.0f}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 _Rate: 1 ETH = ${usd_rate:,.2f} / Rp {idr_rate:,.0f}_"
    )
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


# ============== Gas Commands ==============

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check current Ethereum gas prices."""
    await update.message.reply_text("⛽ Mengambil data gas...")
    
    gas_data = await gas_api.get_gas_price()
    message = gas_api.format_gas_price(gas_data)
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def gasalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set gas price alert."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/gasalert <gwei> [below/above]`\n\n"
            "Contoh:\n"
            "• `/gasalert 25` - Alert ketika gas < 25 gwei\n"
            "• `/gasalert 100 above` - Alert ketika gas > 100 gwei",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        target_gwei = float(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Target gwei harus berupa angka.")
        return
    
    alert_type = context.args[1].lower() if len(context.args) > 1 else "below"
    if alert_type not in ["below", "above"]:
        alert_type = "below"
    
    user_id = update.effective_user.id
    success = db.add_gas_alert(user_id, target_gwei, alert_type)
    
    type_text = "di bawah" if alert_type == "below" else "di atas"
    
    if success:
        await update.message.reply_text(
            f"⛽ Gas Alert berhasil diset!\n\n"
            f"Alert: Gas {type_text} **{target_gwei} gwei**\n\n"
            f"Anda akan mendapat notifikasi ketika gas mencapai target.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Gas alert dengan setting ini sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


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


async def check_percentage_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check percentage-based alerts."""
    alerts = db.get_all_percentage_alerts()
    
    for user_id, collection_slug, percentage, direction in alerts:
        try:
            # Get current price
            stats = await opensea_api.get_collection_stats(collection_slug)
            if stats and "error" not in stats:
                total = stats.get("total", {})
                current_price = total.get("floor_price", 0)
                symbol = total.get("floor_price_symbol", "ETH")
                
                # Get oldest recorded price
                old_price = db.get_oldest_price(collection_slug)
                
                if old_price and old_price > 0:
                    change_pct = ((current_price - old_price) / old_price) * 100
                    
                    should_trigger = False
                    if direction == "up" and change_pct >= percentage:
                        should_trigger = True
                    elif direction == "down" and change_pct <= -percentage:
                        should_trigger = True
                    elif direction == "both" and abs(change_pct) >= percentage:
                        should_trigger = True
                    
                    if should_trigger:
                        sign = "+" if change_pct > 0 else ""
                        trend = "📈 NAIK" if change_pct > 0 else "📉 TURUN"
                        
                        message = (
                            f"🚨 **Percentage Alert!**\n\n"
                            f"Koleksi: `{collection_slug}`\n"
                            f"Perubahan: {trend} **{sign}{change_pct:.1f}%**\n"
                            f"Harga lama: {old_price:.4f} {symbol}\n"
                            f"Harga sekarang: **{current_price:.4f} {symbol}**"
                        )
                        
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=message,
                                parse_mode=ParseMode.MARKDOWN
                            )
                            db.deactivate_percentage_alert(user_id, collection_slug, percentage)
                        except Exception as e:
                            logger.error(f"Failed to send percentage alert to user {user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error checking percentage alert for {collection_slug}: {e}")


async def check_volume_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check volume spike alerts."""
    alerts = db.get_all_volume_alerts()
    
    for user_id, collection_slug, multiplier in alerts:
        try:
            stats = await opensea_api.get_collection_stats(collection_slug)
            if stats and "error" not in stats:
                # Get current volume from intervals
                intervals = stats.get("intervals", [])
                current_volume = 0
                
                for interval in intervals:
                    if interval.get("interval") == "one_day":
                        current_volume = interval.get("volume", 0) or 0
                        break
                
                # Get average volume
                avg_volume = db.get_average_volume(collection_slug)
                
                if avg_volume and avg_volume > 0 and current_volume > 0:
                    spike_ratio = current_volume / avg_volume
                    
                    if spike_ratio >= multiplier:
                        symbol = stats.get("total", {}).get("floor_price_symbol", "ETH")
                        
                        message = (
                            f"🚨 **Volume Spike Alert!**\n\n"
                            f"Koleksi: `{collection_slug}`\n"
                            f"Volume 24h: **{current_volume:.2f} {symbol}**\n"
                            f"Rata-rata: {avg_volume:.2f} {symbol}\n"
                            f"Spike: **{spike_ratio:.1f}x** 📊"
                        )
                        
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=message,
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception as e:
                            logger.error(f"Failed to send volume alert to user {user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error checking volume alert for {collection_slug}: {e}")


async def check_gas_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check gas price alerts."""
    alerts = db.get_all_gas_alerts()
    
    if not alerts:
        return
    
    gas_data = await gas_api.get_gas_price()
    if gas_data and "error" not in gas_data:
        current_gas = gas_data.get("average", 0)
        
        for user_id, target_gwei, alert_type in alerts:
            should_trigger = False
            if alert_type == "below" and current_gas < target_gwei:
                should_trigger = True
            elif alert_type == "above" and current_gas > target_gwei:
                should_trigger = True
            
            if should_trigger:
                type_text = "di bawah" if alert_type == "below" else "di atas"
                
                message = (
                    f"⛽ **Gas Alert!**\n\n"
                    f"Gas saat ini: **{current_gas:.1f} gwei**\n"
                    f"Target: {type_text} {target_gwei} gwei ✅"
                )
                
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    db.deactivate_gas_alert(user_id, target_gwei)
                except Exception as e:
                    logger.error(f"Failed to send gas alert to user {user_id}: {e}")


async def record_price_history(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to record price history for all tracked collections."""
    # Get unique collections from all tracked + alerts
    tracked = db.get_all_tracked_collections()
    collections = set(slug for _, slug in tracked)
    
    for collection_slug in collections:
        try:
            stats = await opensea_api.get_collection_stats(collection_slug)
            if stats and "error" not in stats:
                total = stats.get("total", {})
                floor_price = total.get("floor_price", 0) or 0
                
                # Get volume data from intervals
                intervals = stats.get("intervals", [])
                volume_24h = 0
                sales_count = 0
                avg_price = 0
                
                for interval in intervals:
                    if interval.get("interval") == "one_day":
                        volume_24h = interval.get("volume", 0) or 0
                        sales_count = interval.get("sales", 0) or 0
                        avg_price = interval.get("average_price", 0) or 0
                        break
                
                db.save_price_history(collection_slug, floor_price, volume_24h, sales_count, avg_price)
        
        except Exception as e:
            logger.error(f"Error recording price history for {collection_slug}: {e}")


# ============== Mint Reminder Commands ==============

async def addmint_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a mint reminder."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/addmint nama | harga | YYYY-MM-DD HH:MM | link`\n\n"
            "Contoh:\n"
            "`Azuki Elementals | 0.5 ETH | 2026-03-01 14:00 | https://azuki.com/mint`\n\n"
            "_Link bersifat opsional._",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    text = " ".join(context.args)
    parts = text.split("|")
    
    if len(parts) < 3:
        await update.message.reply_text(
            "❌ Format salah. Pisahkan dengan `|`\n\n"
            "_Format:_ `nama | harga | YYYY-MM-DD HH:MM | link`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    nft_name = parts[0].strip()
    mint_price = parts[1].strip()
    date_str = parts[2].strip()
    mint_link = parts[3].strip() if len(parts) > 3 else ""
    
    try:
        mint_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Format tanggal salah.\nGunakan: `YYYY-MM-DD HH:MM`\n_Contoh:_ `2026-03-01 14:00`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    user_id = update.effective_user.id
    success = db.add_mint_reminder(user_id, nft_name, mint_price, date_str, mint_link)
    
    if success:
        msg = (f"🗓 Mint Reminder berhasil ditambahkan!\n\n"
               f"NFT: *{nft_name}*\n"
               f"Price: {mint_price}\n"
               f"Date: `{date_str}`\n")
        if mint_link:
            msg += f"Link: {mint_link}\n"
        msg += "\n_Bot akan mengingatkan 30 menit & 5 menit sebelum mint._"
    else:
        msg = "❌ Gagal menambahkan reminder."
    
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def mints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active mint reminders."""
    user_id = update.effective_user.id
    reminders = db.get_mint_reminders(user_id)
    
    if not reminders:
        await update.message.reply_text(
            "🗓 Anda belum memiliki mint reminder.\n"
            "Gunakan `/addmint` untuk menambahkan.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    message = "🗓 **Mint Reminders Aktif:**\n\n"
    for rid, name, price, mdate, link in reminders:
        message += f"**#{rid} — {name}**\n"
        message += f"├ 💰 Price: {price}\n"
        message += f"├ 📅 Date: `{mdate}`\n"
        if link:
            message += f"└ 🔗 [Mint Link]({link})\n"
        else:
            message += f"└ 🔗 No link\n"
        message += "\n"
    
    message += "_Hapus dengan_ `/removemint <id>`"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def removemint_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a mint reminder."""
    if not context.args:
        await update.message.reply_text(
            "❌ Format: `/removemint <id>`\n"
            "Cek ID reminder di `/mints`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        reminder_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID harus berupa angka. Cek ID di /mints")
        return
    
    user_id = update.effective_user.id
    success = db.remove_mint_reminder(user_id, reminder_id)
    
    if success:
        await update.message.reply_text(f"✅ Reminder #{reminder_id} berhasil dihapus.")
    else:
        await update.message.reply_text(f"❌ Reminder #{reminder_id} tidak ditemukan.")


async def check_mint_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check and send mint reminders."""
    reminders = db.get_upcoming_reminders()
    now = datetime.now()
    
    for rid, user_id, nft_name, mint_price, mint_date_str, mint_link, reminded_30, reminded_5 in reminders:
        try:
            mint_dt = datetime.strptime(mint_date_str, "%Y-%m-%d %H:%M")
            time_until = mint_dt - now
            minutes_until = time_until.total_seconds() / 60
            
            # Deactivate if mint time has already passed
            if minutes_until < -5:
                db.deactivate_mint_reminder(rid)
                continue
            
            should_send = False
            reminder_type = ""
            
            # 30 minute reminder
            if not reminded_30 and 25 <= minutes_until <= 35:
                should_send = True
                reminder_type = "30min"
                time_text = "⏰ **30 menit lagi!**"
            
            # 5 minute reminder
            elif not reminded_5 and 0 <= minutes_until <= 8:
                should_send = True
                reminder_type = "5min"
                time_text = "🚨 **5 menit lagi!**"
            
            if should_send:
                message = (
                    f"🗓 **Mint Reminder!**\n\n"
                    f"{time_text}\n\n"
                    f"NFT: **{nft_name}**\n"
                    f"Price: {mint_price}\n"
                    f"Waktu: `{mint_date_str}`\n"
                )
                if mint_link:
                    message += f"\n🔗 [Mint Link]({mint_link})"
                
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN,
                        disable_web_page_preview=True
                    )
                    db.mark_reminded(rid, reminder_type)
                except Exception as e:
                    logger.error(f"Failed to send mint reminder to user {user_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error checking mint reminder {rid}: {e}")


def main() -> None:
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN tidak ditemukan!")
        print("Silakan copy .env.example ke .env dan isi dengan token bot Anda.")
        return
    
    # Start health check server in background thread (for Koyeb)
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("floor", floor_command))
    application.add_handler(CommandHandler("track", track_command))
    application.add_handler(CommandHandler("untrack", untrack_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("alert", alert_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("delalert", delalert_command))
    application.add_handler(CommandHandler("palert", palert_command))
    application.add_handler(CommandHandler("volume", volume_command))
    application.add_handler(CommandHandler("valert", valert_command))
    application.add_handler(CommandHandler("addnft", addnft_command))
    application.add_handler(CommandHandler("removenft", removenft_command))
    application.add_handler(CommandHandler("portfolio", portfolio_command))
    application.add_handler(CommandHandler("gas", gas_command))
    application.add_handler(CommandHandler("gasalert", gasalert_command))
    application.add_handler(CommandHandler("ethprice", ethprice_command))
    application.add_handler(CommandHandler("convert", convert_command))
    application.add_handler(CommandHandler("addmint", addmint_command))
    application.add_handler(CommandHandler("mints", mints_command))
    application.add_handler(CommandHandler("removemint", removemint_command))
    
    # Add callback handler for inline keyboard buttons
    application.add_handler(CallbackQueryHandler(button_handler))
    
    # Add message handler for pending input from menu buttons
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, pending_input_handler
    ))
    
    # Add background jobs
    job_queue = application.job_queue
    job_queue.run_repeating(check_alerts, interval=ALERT_CHECK_INTERVAL, first=60)
    job_queue.run_repeating(check_percentage_alerts, interval=ALERT_CHECK_INTERVAL, first=90)
    job_queue.run_repeating(check_volume_alerts, interval=ALERT_CHECK_INTERVAL, first=120)
    job_queue.run_repeating(check_gas_alerts, interval=ALERT_CHECK_INTERVAL, first=150)
    job_queue.run_repeating(check_mint_reminders, interval=60, first=30)
    job_queue.run_repeating(record_price_history, interval=PRICE_HISTORY_INTERVAL, first=300)
    
    # Start the bot
    print("🚀 Bot started! Press Ctrl+C to stop.")
    print("📊 Features: Price alerts, % alerts, Volume alerts, Portfolio, Gas alerts, ETH/IDR converter, Mint reminders")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
