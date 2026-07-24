import asyncio
import logging
import os
import re
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

from config import (
    TELEGRAM_BOT_TOKEN,
    ALERT_CHECK_INTERVAL,
    PRICE_HISTORY_INTERVAL,
    VOLUME_ALERT_COOLDOWN_SECONDS,
    VOLUME_SPIKE_MULTIPLIER,
)
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


def _parse_db_timestamp(value: str | None) -> datetime | None:
    """Parse SQLite CURRENT_TIMESTAMP values for lightweight cooldown checks."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def _is_in_cooldown(last_triggered_at: str | None, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return False
    last_triggered = _parse_db_timestamp(last_triggered_at)
    if not last_triggered:
        return False
    return datetime.utcnow() - last_triggered < timedelta(seconds=cooldown_seconds)


def _price_alert_condition_met(alert_type: str, current_price: float, target_price: float) -> bool:
    if alert_type == "below":
        return current_price < target_price
    if alert_type == "above":
        return current_price > target_price
    return False


def _price_alert_crossed(alert_type: str, last_price: float | None, target_price: float) -> bool:
    if last_price is None or last_price <= 0:
        return True
    if alert_type == "below":
        return last_price >= target_price
    if alert_type == "above":
        return last_price <= target_price
    return False


def _normalize_basis(value: str | None) -> str:
    """Map a user/keyword value to the stored basis: 'floor' or 'top_offer'."""
    if value and value.lower() in ("offer", "top_offer", "topoffer", "top-offer", "bid"):
        return "top_offer"
    return "floor"


def _basis_label(price_basis: str) -> str:
    return "Top Offer" if price_basis == "top_offer" else "Floor"


async def _current_offer_price(slug: str) -> tuple[float, str]:
    """Return (per-item top offer, currency) for a collection, or (0, 'WETH')."""
    offer = await opensea_api.get_top_collection_offer(slug)
    if not offer or "error" in offer:
        return 0.0, "WETH"
    return (offer.get("value", 0) or 0), offer.get("symbol", "WETH")


def _format_empty_state(title: str, body: str, action_hint: str = "") -> str:
    text = f"{title}\n\n{body}"
    if action_hint:
        text += f"\n\n{action_hint}"
    return text


def _escape_md_text(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("[", "\\[")
        .replace("`", "'")
    )


def _compact_slug(slug: str, max_len: int = 34) -> str:
    if len(slug) <= max_len:
        return slug
    return f"{slug[:max_len - 1]}…"


def _slug_code(slug: str, max_len: int = 26) -> str:
    """Slug as a Markdown code span: monospace, no auto-linking of digits."""
    return "`" + _compact_slug(slug, max_len).replace("`", "'") + "`"


_ETH_AMOUNT_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:eth)?\s*$", re.IGNORECASE)


def _parse_eth_amount(text: str):
    """Parse a bare ETH amount like '0.5', '2,5', or '1 eth'. Returns float or None."""
    match = _ETH_AMOUNT_RE.match(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def _format_eth_conversion(amount: float, eth_data: dict) -> str:
    usd_rate = eth_data.get("usd", 0) or 0
    idr_rate = eth_data.get("idr", 0) or 0
    return (
        "💱 *ETH Converter*\n"
        f"Amount: *{amount:g} ETH*\n\n"
        "📊 *Result*\n"
        f"🇺🇸 USD: *${amount * usd_rate:,.2f}*\n"
        f"🇮🇩 IDR: *Rp {amount * idr_rate:,.0f}*\n\n"
        f"💹 _1 ETH = ${usd_rate:,.2f} / Rp {idr_rate:,.0f}_"
    )


def _convert_result_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Konversi Lagi", callback_data="cmd_convert"),
         InlineKeyboardButton("💰 Harga ETH", callback_data="cmd_ethprice")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])


async def _maybe_quick_convert(update) -> bool:
    """If the message is a bare ETH amount, reply with USD/IDR. Returns True if handled."""
    amount = _parse_eth_amount(update.message.text)
    if amount is None:
        return False
    eth_data = await price_api.get_eth_price()
    if not eth_data or "error" in eth_data:
        await update.message.reply_text("❌ Gagal mengambil harga ETH. Coba lagi.")
        return True
    await update.message.reply_text(
        _format_eth_conversion(amount, eth_data),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_convert_result_keyboard(),
    )
    return True


def _format_watchlist(collections: list[str]) -> str:
    if not collections:
        return _format_empty_state(
            "📌 *Watchlist Kosong*",
            "Belum ada koleksi yang dipantau.",
            "Mulai dengan `/track slug` atau cek cepat pakai `.p slug`."
        )

    lines = [
        "📌 *Watchlist*",
        f"{len(collections)} koleksi dipantau",
        "",
    ]
    for i, slug in enumerate(collections, 1):
        display_slug = _escape_md_text(_compact_slug(slug))
        lines.append(f"{i}. *{display_slug}*")
    lines.extend([
        "",
        "🔎 Detail cepat: `.p slug`",
        "📎 Pendekkan slug: `.alias nama slug-asli`",
    ])
    return "\n".join(lines)


def _format_tracked_price_results(results: list[tuple[str, str, float | None, str, str | None]],
                                  idr_rate: float = 0,
                                  title: str = "📊 *Tracked Floors*",
                                  label: str = "Floor") -> str:
    lines = [
        title,
        f"{len(results)} koleksi dipantau",
        "",
    ]
    for index, (slug, status, price, symbol, error) in enumerate(results, 1):
        display_slug = _slug_code(slug)
        if status == "ok" and price is not None:
            idr_text = f" • Rp {price * idr_rate:,.0f}" if idr_rate else ""
            lines.append(f"*{index}.* {display_slug}")
            lines.append(f"    {label}: *{price:.4f} {symbol}*{idr_text}")
        else:
            safe_error = _escape_md_text(error or "Gagal mengambil data")
            lines.append(f"*{index}.* {display_slug}")
            lines.append(f"    ❌ {safe_error}")
    lines.append("")
    if idr_rate:
        lines.append(f"💱 _1 ETH = Rp {idr_rate:,.0f}_")
    lines.append("👇 _Tap nomor koleksi untuk aksi._")
    return "\n".join(lines).strip()


def _format_alerts_overview(price_alerts, percent_alerts, volume_alerts, gas_alerts) -> str:
    total_count = len(price_alerts) + len(percent_alerts) + len(volume_alerts) + len(gas_alerts)
    if total_count == 0:
        return _format_empty_state(
            "🔔 *Alert Center*",
            "Belum ada alert aktif.",
            "Buat alert dari menu atau pakai `/alert slug price`."
        )

    lines = [
        "🔔 *Alert Center*",
        f"Active: *{total_count}* alert",
        "",
    ]

    if price_alerts:
        lines.append("💰 *Price Alerts*")
        for aid, slug, price, alert_type, recurring, price_basis in price_alerts:
            direction = "below" if alert_type == "below" else "above"
            basis_tag = "🏷 offer" if price_basis == "top_offer" else "💰 floor"
            repeat_badge = " • recurring" if recurring else ""
            lines.append(f"• `#{aid}` `{slug}` {basis_tag} {direction} *{price} ETH*{repeat_badge}")
        lines.append("")

    if percent_alerts:
        lines.append("📊 *Percentage Alerts*")
        for aid, slug, percent, direction, recurring in percent_alerts:
            repeat_badge = " • recurring" if recurring else ""
            lines.append(f"• `#{aid}` `{slug}` {direction} *{percent}%*{repeat_badge}")
        lines.append("")

    if volume_alerts:
        lines.append("💎 *Volume Alerts*")
        for aid, slug, multiplier in volume_alerts:
            lines.append(f"• `#{aid}` `{slug}` spike *{multiplier}x*")
        lines.append("")

    if gas_alerts:
        lines.append("⛽ *Gas Alerts*")
        for aid, gwei, alert_type in gas_alerts:
            lines.append(f"• `#{aid}` {alert_type} *{gwei} gwei*")
        lines.append("")

    lines.append("_Tap_ 🗑 _di bawah untuk hapus._")
    return "\n".join(lines).strip()


def _format_mint_reminders(reminders) -> str:
    if not reminders:
        return _format_empty_state(
            "🗓 *Mint Reminders*",
            "Belum ada mint yang dijadwalkan.",
            "Tambah dengan `/addmint nama | harga | YYYY-MM-DD HH:MM | link`."
        )

    lines = [
        "🗓 *Mint Reminders*",
        f"Active: *{len(reminders)}* reminder",
        "",
    ]
    for rid, name, price, mdate, link in reminders:
        lines.append(f"*#{rid} — {name}*")
        lines.append(f"💰 Price: *{price}*")
        lines.append(f"📅 Date: `{mdate}`")
        if link:
            lines.append(f"🔗 [Mint Link]({link})")
        else:
            lines.append("🔗 No link")
        lines.append("")

    lines.append("_Hapus dengan_ `/removemint id`")
    return "\n".join(lines).strip()


def _format_price_alert_created(slug: str, current_price: float, symbol: str,
                                target_price: float, alert_type: str, is_recurring: bool,
                                price_basis: str = "floor") -> str:
    direction_text = "di bawah" if alert_type == "below" else "di atas"
    direction_emoji = "📉" if alert_type == "below" else "📈"
    is_offer = price_basis == "top_offer"
    basis_label = _basis_label(price_basis)
    basis_emoji = "🏷" if is_offer else "💰"
    now_text = (
        f"{basis_emoji} {basis_label} sekarang: *{current_price:.4f} {symbol}*"
        if current_price > 0
        else f"{basis_emoji} {basis_label} sekarang: _belum ada_"
    )
    lines = [
        "✅ *Price Alert Created*",
        f"Koleksi: `{slug}`",
        f"🎯 Basis: *{basis_label}*",
        "",
        "📊 *Market*",
        now_text,
        f"{direction_emoji} Target: {basis_label} {direction_text} *{target_price} {symbol}*",
    ]
    if is_recurring:
        lines.append("🔁 Recurring: *on*")
    return "\n".join(lines)


def _format_percent_alert_created(slug: str, ref_price: float, symbol: str,
                                  percentage: float, direction: str, is_recurring: bool) -> str:
    direction_text = {"up": "📈 naik", "down": "📉 turun", "both": "↕️ naik/turun"}
    lines = [
        "✅ *Percentage Alert Created*",
        f"Koleksi: `{slug}`",
        "",
        "📊 *Market*",
        f"💰 Harga referensi: *{ref_price:.4f} {symbol}*",
        f"{direction_text[direction]} *{percentage}%*",
    ]
    if is_recurring:
        lines.append("🔁 Recurring: *on*")
    return "\n".join(lines)


def _format_volume_alert_created(slug: str, multiplier: float) -> str:
    return (
        "✅ *Volume Alert Created*\n"
        f"Koleksi: `{slug}`\n\n"
        "📊 *Signal*\n"
        f"💎 Trigger: volume *{multiplier}x* dari rata-rata"
    )


def _format_portfolio_item_added(slug: str, quantity: int, buy_price: float) -> str:
    total_cost = quantity * buy_price
    return (
        "✅ *Portfolio Updated*\n"
        f"Koleksi: `{slug}`\n\n"
        "📊 *Position*\n"
        f"🧾 Quantity: *{quantity} NFT*\n"
        f"💰 Buy Price: *{buy_price:.4f} ETH*\n"
        f"📦 Cost Basis: *{total_cost:.4f} ETH*"
    )


def _format_gas_alert_created(target_gwei: float, alert_type: str) -> str:
    type_text = "di bawah" if alert_type == "below" else "di atas"
    return (
        "✅ *Gas Alert Created*\n"
        "Ethereum network fee monitor\n\n"
        "📊 *Signal*\n"
        f"⛽ Alert saat gas {type_text} *{target_gwei:g} gwei*"
    )


def _format_mint_added(nft_name: str, mint_price: str, date_str: str, mint_link: str = "") -> str:
    lines = [
        "✅ *Mint Reminder Created*",
        f"NFT: *{nft_name}*",
        "",
        "📊 *Mint Info*",
        f"💰 Price: *{mint_price}*",
        f"📅 Date: `{date_str}`",
    ]
    if mint_link:
        lines.append(f"🔗 Link: {mint_link}")
    lines.append("")
    lines.append("_Reminder dikirim 30 menit dan 5 menit sebelum mint._")
    return "\n".join(lines)


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
        [InlineKeyboardButton("🔍 Cek 1 Koleksi", callback_data="cmd_floor"),
         InlineKeyboardButton("📋 Watchlist (nama)", callback_data="cmd_list")],
        [InlineKeyboardButton("📊 Cek Floor", callback_data="cmd_check"),
         InlineKeyboardButton("🏷 Cek Offer", callback_data="cmd_check_offer")],
        [InlineKeyboardButton("📌 Track", callback_data="cmd_track"),
         InlineKeyboardButton("🗑 Untrack", callback_data="cmd_untrack")],
        [InlineKeyboardButton("💎 Volume", callback_data="cmd_volume"),
         InlineKeyboardButton("⬅️ Kembali", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def alerts_menu_keyboard():
    """Sub-menu for alert commands."""
    keyboard = [
        [InlineKeyboardButton("➕ Buat Alert Baru", callback_data="menu_create_alert")],
        [InlineKeyboardButton("🔔 Alerts Saya", callback_data="cmd_alerts")],
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
        [InlineKeyboardButton("🏷 Offer < Target", callback_data="cmd_alert_offer_below"),
         InlineKeyboardButton("🏷 Offer > Target", callback_data="cmd_alert_offer_above")],
        [InlineKeyboardButton("📊 % Perubahan", callback_data="cmd_palert"),
         InlineKeyboardButton("💎 Volume Spike", callback_data="cmd_valert")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts")],
    ]
    return InlineKeyboardMarkup(keyboard)


MAX_ALERT_DELETE_BUTTONS = 15


def alerts_overview_keyboard(price_alerts, percent_alerts, volume_alerts, gas_alerts):
    """One 🗑 delete button per alert (capped), plus footer navigation."""
    rows = []
    count = 0

    def _add(prefix, aid, label, kind):
        nonlocal count
        if count < MAX_ALERT_DELETE_BUTTONS:
            rows.append([InlineKeyboardButton(
                f"🗑 {prefix} #{aid} · {label}", callback_data=f"del_{kind}_{aid}"
            )])
            count += 1

    for aid, slug, *_ in price_alerts:
        _add("💰", aid, _compact_slug(slug, 16), "price")
    for aid, slug, *_ in percent_alerts:
        _add("📊", aid, _compact_slug(slug, 16), "percent")
    for aid, slug, *_ in volume_alerts:
        _add("💎", aid, _compact_slug(slug, 16), "volume")
    for aid, gwei, *_ in gas_alerts:
        _add("⛽", aid, f"{gwei} gwei", "gas")

    rows.append([
        InlineKeyboardButton("➕ Tambah", callback_data="menu_create_alert"),
        InlineKeyboardButton("🏠 Menu", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(rows)


def _alerts_overview_payload(user_id: int):
    """Build (text, keyboard) for the alert list — shared by view + delete flows."""
    price_alerts = db.get_user_alerts(user_id)
    percent_alerts = db.get_percentage_alerts(user_id)
    volume_alerts = db.get_volume_alerts(user_id)
    gas_alerts = db.get_gas_alerts(user_id)
    text = _format_alerts_overview(price_alerts, percent_alerts, volume_alerts, gas_alerts)
    if price_alerts or percent_alerts or volume_alerts or gas_alerts:
        keyboard = alerts_overview_keyboard(price_alerts, percent_alerts, volume_alerts, gas_alerts)
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Buat Alert", callback_data="menu_create_alert"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
    return text, keyboard


MAX_WATCHLIST_BUTTONS = 40


def _check_cb(mode: str, sort: str) -> str:
    """Callback data for a check view of the given mode + sort."""
    base = "cmd_check_offer" if mode == "offer" else "cmd_check"
    return base + ("_hi" if sort == "hi" else "")


def _check_keyboard(ordered_items, mode: str = "floor", sort: str = "add"):
    """Numbered per-collection buttons (keyed by stable row_id) + refresh,
    floor↔offer toggle, sort toggle, and nav."""
    rows, row = [], []
    for pos, (row_id, _slug) in enumerate(ordered_items[:MAX_WATCHLIST_BUTTONS]):
        row.append(InlineKeyboardButton(str(pos + 1), callback_data=f"wlmenu_{row_id}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    other_mode = "offer" if mode == "floor" else "floor"
    toggle_label = "🏷 Cek Offer" if mode == "floor" else "📊 Cek Floor"
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=_check_cb(mode, sort)),
        InlineKeyboardButton(toggle_label, callback_data=_check_cb(other_mode, sort)),
    ])

    if sort == "hi":
        sort_btn = InlineKeyboardButton("↕️ Urut Track", callback_data=_check_cb(mode, "add"))
    else:
        sort_btn = InlineKeyboardButton("⬇️ Termahal", callback_data=_check_cb(mode, "hi"))
    rows.append([
        sort_btn,
        InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list"),
    ])
    rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


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
    "Live NFT market monitor\n\n"
    "📊 *Tools*\n"
    "💰 Floor, volume, dan recent sales\n"
    "🔔 Price, volume, dan gas alerts\n"
    "💼 Portfolio & mint reminders\n\n"
    "Pilih menu di bawah."
)

PRICE_MENU_TEXT = (
    "📊 *Price & Tracking*\n"
    "Floor, watchlist, dan market snapshot\n\n"
    "📋 *Watchlist* — daftar nama koleksi (instan)\n"
    "📊 *Cek Floor* / 🏷 *Cek Offer* — harga live watchlist\n"
    "📌 Track banyak: `/track azuki pudgypenguins`\n"
    "🔎 Cepat 1 koleksi: `.p slug`"
)

ALERTS_MENU_TEXT = (
    "🔔 *Alert Center*\n"
    "Kelola sinyal market yang perlu dipantau\n\n"
    "💰 Floor & 🏷 top offer\n"
    "📊 % change & 💎 volume spike\n"
    "⛽ Gas threshold"
)

CREATE_ALERT_MENU_TEXT = (
    "📊 *Buat Alert Baru*\n"
    "Pilih sinyal yang ingin bot awasi\n\n"
    "📉 Floor di bawah/atas target\n"
    "🏷 Top offer di bawah/atas target\n"
    "📊 Perubahan persentase\n"
    "💎 Volume spike"
)

PORTFOLIO_MENU_TEXT = (
    "💼 *Portfolio*\n"
    "Holdings, cost basis, P/L, dan ROI\n\n"
    "➕ Tambah posisi NFT\n"
    "📊 Cek valuasi terbaru"
)

GAS_MENU_TEXT = (
    "⛽ *Gas Fee*\n"
    "Ethereum network fee monitor\n\n"
    "🔥 Cek low/average/fast\n"
    "⏰ Alert saat gas masuk target"
)

CONVERTER_MENU_TEXT = (
    "💱 *ETH ↔ IDR Converter*\n"
    "Live ETH rate dan kalkulator cepat\n\n"
    "💰 Harga ETH sekarang\n"
    "🔄 Konversi jumlah ETH ke fiat\n\n"
    "_Tip: kirim angka saja, mis._ `0.5` _atau_ `2,5`"
)

HELP_TEXT = (
    "📖 *Bantuan*\n"
    "Cara cepat pakai bot\n\n"
    "🔎 Floor: `.p slug`\n"
    "📌 Track (banyak): `/track slug1 slug2 ...`\n"
    "🔔 Alert: `/alert slug price`\n"
    "🏷 Offer alert: `/alert slug price above offer`\n"
    "💱 Convert: ketik angka ETH saja (mis. `0.5`)\n"
    "📎 Alias: `.alias pendek slug-asli`\n\n"
    "Slug ada di URL OpenSea, contoh:\n"
    "`opensea.io/collection/boredapeyachtclub`"
)

# Prompts shown when asking for user input
INPUT_PROMPTS = {
    "floor": "🔍 *Floor Price*\nKetik slug koleksi NFT.\n\nContoh: `boredapeyachtclub`",
    "track": "📌 *Track Koleksi*\nKetik satu atau beberapa slug (pisah spasi/koma).\n\nContoh: `azuki` atau `azuki pudgypenguins boredapeyachtclub`",
    "untrack": "🗑 *Untrack Koleksi*\nKetik satu atau beberapa slug (pisah spasi/koma).\n\nContoh: `azuki` atau `azuki pudgypenguins`",
    "volume": "💎 *Volume*\nKetik slug koleksi NFT.\n\nContoh: `boredapeyachtclub`",
    "alert_below": "📉 *Floor Alert*\nFloor di bawah target.\n\nFormat: `slug harga [repeat]`\nContoh: `boredapeyachtclub 50`",
    "alert_above": "📈 *Floor Alert*\nFloor di atas target.\n\nFormat: `slug harga [repeat]`\nContoh: `azuki 20`",
    "alert_offer_below": "🏷 *Top Offer Alert*\nTop offer di bawah target.\n\nFormat: `slug harga [repeat]`\nContoh: `boredapeyachtclub 8`",
    "alert_offer_above": "🏷 *Top Offer Alert*\nTop offer di atas target.\n\nFormat: `slug harga [repeat]`\nContoh: `azuki 1`",
    "alert": "⚡ *Price Alert*\nKetik slug dan harga target.\n\nFormat: `slug harga [above/below] [floor/offer] [repeat]`\nContoh: `azuki 20 above offer`",
    "palert": "📊 *Percentage Alert*\nKetik slug, persen, dan arah.\n\nFormat: `slug persen [up/down/both] [repeat]`\nContoh: `azuki 10 up`",
    "valert": "💎 *Volume Alert*\nKetik slug dan multiplier.\n\nFormat: `slug [multiplier]`\nContoh: `azuki 3`",
    "addnft": "➕ *Tambah NFT*\nKetik slug, jumlah, dan harga beli.\n\nFormat: `slug jumlah buy_price`\nContoh: `azuki 2 15.5`",
    "removenft": "➖ *Hapus NFT*\nKetik satu atau beberapa slug (pisah spasi/koma).\n\nContoh: `azuki` atau `azuki pudgypenguins`",
    "gasalert": "⏰ *Gas Alert*\nKetik target gwei dan tipe.\n\nFormat: `gwei [below/above]`\nContoh: `25 below`",
    "addmint": "🗓 *Mint Reminder*\nKetik info mint NFT.\n\nFormat: `nama | harga | YYYY-MM-DD HH:MM | link`\nContoh: `Azuki Elementals | 0.5 ETH | 2026-03-01 14:00 | https://azuki.com/mint`",
    "removemint": "🗑 *Hapus Mint Reminder*\nKetik ID reminder yang ingin dihapus.\n\nCek ID: `/mints`",
    "convert": "🔄 *ETH Converter*\nKetik jumlah ETH.\n\nContoh: `0.5` atau `2.5`",
    "delalert": "🗑 *Hapus Alert*\nKetik tipe dan ID alert.\n\nFormat: `tipe ID`\nTipe: `price` / `persen` / `volume` / `gas`\nContoh: `price 5`",
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

    # Keep the interactive menu at the bottom of the chat: delete the tapped
    # message and resend a fresh one, so it drops below any messages that
    # arrived in between. Falls back gracefully if the old message can't be
    # deleted (e.g. too old).
    current = query.message

    async def show(text, reply_markup=None, disable_web_page_preview=False):
        nonlocal current
        try:
            await current.delete()
        except Exception:
            pass
        current = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=disable_web_page_preview,
            disable_notification=True,
        )
        return current

    def cancel_kb():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batal", callback_data="menu_main")]
        ])

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
        await show(text, keyboard)
        return

    # ---- 1-tap alert delete ----
    if data.startswith("del_"):
        remove_map = {
            "price": db.remove_alert_by_id,
            "percent": db.remove_percent_alert_by_id,
            "volume": db.remove_volume_alert_by_id,
            "gas": db.remove_gas_alert_by_id,
        }
        try:
            _, kind, aid_str = data.split("_", 2)
            aid = int(aid_str)
        except (ValueError, IndexError):
            kind, aid = None, None
        remove_func = remove_map.get(kind)
        if remove_func and aid is not None:
            remove_func(user_id, aid)
        text, keyboard = _alerts_overview_payload(user_id)
        await show(text, keyboard)
        return

    # ---- No-arg commands: execute directly ----
    if data == "cmd_list":
        collections = db.get_tracked_collections(user_id)
        text = _format_watchlist(collections)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Cek Floor", callback_data="cmd_check"),
             InlineKeyboardButton("🏷 Cek Offer", callback_data="cmd_check_offer")],
            [InlineKeyboardButton("📌 Track Baru", callback_data="cmd_track")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_price"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await show(text, keyboard)
        return

    if data in ("cmd_check", "cmd_check_hi", "cmd_check_offer", "cmd_check_offer_hi"):
        mode = "offer" if "offer" in data else "floor"
        sort = "hi" if data.endswith("_hi") else "add"
        if not db.get_tracked_collections(user_id):
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📌 Track", callback_data="cmd_track"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_price")]
            ])
            await show(_format_watchlist([]), keyboard)
            return
        msg = await show("🏷 Mengambil top offer..." if mode == "offer" else "🔍 Mengambil floor price...")
        text, ordered_items = await _build_check(user_id, mode, sort)
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                            reply_markup=_check_keyboard(ordered_items, mode, sort))
        return

    # ---- Per-collection actions from the numbered watchlist buttons (by row id) ----
    if data.startswith(("wlmenu_", "wldetail_", "wlfloor_", "wloffer_", "wluntrack_")):
        try:
            action, id_str = data.rsplit("_", 1)
            row_id = int(id_str)
        except (ValueError, IndexError):
            action, row_id = None, -1
        slug = db.get_tracked_slug_by_id(user_id, row_id) if row_id >= 0 else None
        if slug is None:
            await show(
                "⚠️ Koleksi tidak ditemukan (mungkin sudah di-untrack).",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("📊 Cek Floor", callback_data="cmd_check"),
                    InlineKeyboardButton("🏠 Menu", callback_data="menu_main"),
                ]])
            )
            return

        if action == "wlmenu":
            await show(
                f"📦 *{_slug_code(slug, 40)}*\n\nPilih aksi:",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Detail", callback_data=f"wldetail_{row_id}")],
                    [InlineKeyboardButton("💰 Floor Alert", callback_data=f"wlfloor_{row_id}"),
                     InlineKeyboardButton("🏷 Offer Alert", callback_data=f"wloffer_{row_id}")],
                    [InlineKeyboardButton("🗑 Untrack", callback_data=f"wluntrack_{row_id}")],
                    [InlineKeyboardButton("⬅️ Ke Daftar", callback_data="cmd_check"),
                     InlineKeyboardButton("🏠 Menu", callback_data="menu_main")],
                ])
            )
            return

        if action == "wldetail":
            msg = await show(f"🔍 Mencari data untuk `{slug}`...")
            text, kb = await _build_floor_overview(slug)
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
                                disable_web_page_preview=True)
            return

        if action in ("wlfloor", "wloffer"):
            basis = "top_offer" if action == "wloffer" else "floor"
            context.user_data["pending_action"] = "alert"
            context.user_data["pending_slug"] = slug
            context.user_data["pending_basis"] = basis
            emoji, label, unit = (
                ("🏷", "Top Offer", "WETH") if basis == "top_offer" else ("💰", "Floor", "ETH")
            )
            await show(
                f"{emoji} *{label} Alert untuk* `{slug}`\n\n"
                f"Ketik harga target ({unit}):\n\n"
                "_Contoh:_ `50` atau `50 above`",
                cancel_kb()
            )
            return

        if action == "wluntrack":
            db.remove_tracked_collection(user_id, slug)
            await show(
                f"🗑 `{slug}` dihapus dari watchlist.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 Cek Floor", callback_data="cmd_check"),
                     InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")],
                ])
            )
            return

    if data == "cmd_alerts":
        text, keyboard = _alerts_overview_payload(user_id)
        await show(text, keyboard)
        return

    if data == "cmd_portfolio":
        portfolio = db.get_portfolio(user_id)
        if not portfolio:
            text = _format_empty_state(
                "💼 *Portfolio*",
                "Belum ada posisi NFT yang tersimpan.",
                "Tambah posisi dengan `/addnft slug jumlah buy_price`."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_portfolio")]
            ])
            await show(text, keyboard)
            return
        msg = await show("💼 Menghitung portofolio Anda...")
        text = await _build_portfolio_text(portfolio)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_portfolio"),
             InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_portfolio"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_gas":
        msg = await show("⛽ Mengambil data gas...")
        gas_data = await gas_api.get_gas_price()
        text = gas_api.format_gas_price(gas_data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_gas"),
             InlineKeyboardButton("⏰ Set Gas Alert", callback_data="cmd_gasalert")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_gas"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_ethprice":
        msg = await show("💱 Mengambil harga ETH...")
        eth_data = await price_api.get_eth_price()
        text = price_api.format_eth_price(eth_data)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_ethprice"),
             InlineKeyboardButton("🔄 Konversi", callback_data="cmd_convert")],
            [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_converter"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if data == "cmd_mints":
        reminders = db.get_mint_reminders(user_id)
        text = _format_mint_reminders(reminders)
        if not reminders:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah Reminder", callback_data="cmd_addmint"),
                 InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts")]
            ])
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Tambah", callback_data="cmd_addmint"),
                 InlineKeyboardButton("🗑 Hapus", callback_data="cmd_removemint")],
                [InlineKeyboardButton("⬅️ Kembali", callback_data="menu_alerts"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
            ])
        await show(text, keyboard, disable_web_page_preview=True)
        return

    # ---- Arg-required commands: prompt for input ----
    if data.startswith("cmd_"):
        action = data[4:]  # e.g. "floor", "track", "alert"
        if action in INPUT_PROMPTS:
            context.user_data["pending_action"] = action
            await show(INPUT_PROMPTS[action], cancel_kb())
            return

    # ---- Quick actions after floor check ----
    if data.startswith("qa_alert_"):
        slug = data[9:]  # extract slug from "qa_alert_<slug>"
        context.user_data["pending_action"] = "alert"
        context.user_data["pending_slug"] = slug
        context.user_data["pending_basis"] = "floor"
        await show(
            f"💰 *Floor Alert untuk* `{slug}`\n\n"
            "Ketik harga target (ETH):\n\n"
            "_Contoh:_ `50` atau `50 above`",
            cancel_kb()
        )
        return

    if data.startswith("qa_offer_"):
        slug = data[9:]  # extract slug from "qa_offer_<slug>"
        context.user_data["pending_action"] = "alert"
        context.user_data["pending_slug"] = slug
        context.user_data["pending_basis"] = "top_offer"
        await show(
            f"🏷 *Top Offer Alert untuk* `{slug}`\n\n"
            "Ketik harga target (WETH):\n\n"
            "_Contoh:_ `8` atau `8 above`",
            cancel_kb()
        )
        return

    if data.startswith("qa_valert_"):
        slug = data[10:]  # extract slug from "qa_valert_<slug>"
        context.user_data["pending_action"] = "valert"
        context.user_data["pending_slug"] = slug
        await show(
            f"📢 *Set Volume Alert untuk* `{slug}`\n\n"
            "Ketik multiplier volume spike:\n\n"
            "_Contoh:_ `2` atau `3`",
            cancel_kb()
        )
        return

    if data.startswith("qa_track_"):
        slug = data[9:]  # extract slug from "qa_track_<slug>"
        if db.add_tracked_collection(user_id, slug):
            text = f"✅ `{slug}` berhasil ditambahkan ke watchlist!"
        else:
            text = f"ℹ️ `{slug}` sudah ada di watchlist Anda."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Lihat Watchlist", callback_data="cmd_list"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await show(text, keyboard)
        return


async def pending_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input for pending actions from menu buttons."""
    action = context.user_data.pop("pending_action", None)
    if not action:
        # No menu action pending — a bare ETH amount becomes a quick conversion.
        await _maybe_quick_convert(update)
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    pending_slug = context.user_data.pop("pending_slug", None)
    pending_basis = context.user_data.pop("pending_basis", None)

    # ---- ETH Converter ----
    if action == "convert":
        amount = _parse_eth_amount(text)
        if amount is None:
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
        await update.message.reply_text(
            _format_eth_conversion(amount, eth_data),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_convert_result_keyboard(),
        )
        return

    # ---- Single-arg commands ----
    if action == "floor":
        slug = text.split()[0].lower()
        await send_floor_overview(update.message, slug)
        return

    if action == "track":
        slugs = _parse_slugs(text)
        if not slugs:
            await update.message.reply_text("❌ Ketik minimal satu slug.")
            return
        if len(slugs) > 1:
            await update.message.reply_text(f"🔍 Memverifikasi {len(slugs)} koleksi...")
        msg = await _batch_track(user_id, slugs)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list"),
             InlineKeyboardButton("📊 Cek Harga", callback_data="cmd_check")],
            [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "untrack":
        slugs = _parse_slugs(text)
        if not slugs:
            await update.message.reply_text("❌ Ketik minimal satu slug.")
            return
        msg = _batch_untrack(user_id, slugs)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list"),
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
        message = opensea_api.format_volume_stats(
            stats, collection_info, previous_volume, collection_slug=slug
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Set Volume Alert", callback_data=f"qa_valert_{slug}"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    if action == "removenft":
        slugs = _parse_slugs(text)
        if not slugs:
            await update.message.reply_text("❌ Ketik minimal satu slug.")
            return
        msg = _batch_remove_portfolio(user_id, slugs)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 Lihat Portofolio", callback_data="cmd_portfolio"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    # ---- Multi-arg commands ----
    if action in ("alert", "alert_below", "alert_above",
                  "alert_offer_below", "alert_offer_above"):
        parts = text.split()

        # Basis + direction are implied by the menu button (or the quick-action
        # that set pending_basis). Generic /alert reads them from text keywords.
        price_basis = pending_basis or ("top_offer" if "offer" in action else "floor")
        alert_type = "above" if action.endswith("above") else "below"

        if pending_slug:
            slug = pending_slug
            try:
                target_price = float(parts[0])
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Masukkan harga target berupa angka.\n_Contoh:_ `50`", parse_mode=ParseMode.MARKDOWN)
                return
            keyword_args = parts[1:]
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
            keyword_args = parts[2:]

        keywords = [p.lower() for p in keyword_args]
        # Generic /alert lets the user pick direction + basis inline.
        if action == "alert":
            for p in keywords:
                if p in ("above", "below"):
                    alert_type = p
                elif p in ("floor", "offer", "top_offer", "topoffer", "top-offer", "bid"):
                    price_basis = _normalize_basis(p)
        is_recurring = "repeat" in keywords

        # Verify the collection exists (floor stats works for any valid slug).
        await update.message.reply_text(f"🔍 Memverifikasi `{slug}`...", parse_mode=ParseMode.MARKDOWN)
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return

        if price_basis == "top_offer":
            current_price, symbol = await _current_offer_price(slug)
        else:
            total = stats.get("total", {}) if stats else {}
            current_price = total.get("floor_price", 0) or 0
            symbol = total.get("floor_price_symbol", "ETH")

        success = db.add_price_alert(user_id, slug, target_price, alert_type,
                                      is_recurring=is_recurring, current_price=current_price,
                                      price_basis=price_basis)
        if success:
            msg = _format_price_alert_created(
                slug, current_price, symbol, target_price, alert_type, is_recurring, price_basis
            )
        else:
            msg = (f"ℹ️ Alert ({_basis_label(price_basis)}) untuk `{slug}` "
                   f"dengan target {target_price} {symbol} ({alert_type}) sudah ada.")
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
        if success:
            msg = _format_percent_alert_created(
                slug, ref_price, symbol, percentage, direction, is_recurring
            )
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
        if pending_slug:
            slug = pending_slug
            multiplier_arg = parts[0] if parts else str(VOLUME_SPIKE_MULTIPLIER)
        else:
            if not parts:
                await update.message.reply_text(
                    "❌ Format: `slug [multiplier]`\n_Contoh:_ `azuki 3`",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            slug = parts[0].lower()
            multiplier_arg = parts[1] if len(parts) > 1 else str(VOLUME_SPIKE_MULTIPLIER)
        try:
            multiplier = float(multiplier_arg)
        except ValueError:
            multiplier = VOLUME_SPIKE_MULTIPLIER
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return
        success = db.add_volume_alert(user_id, slug, multiplier)
        if success:
            msg = _format_volume_alert_created(slug, multiplier)
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
            msg = _format_portfolio_item_added(slug, quantity, buy_price)
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
        if success:
            msg = _format_gas_alert_created(target_gwei, alert_type)
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
            msg = _format_mint_added(nft_name, mint_price, date_str, mint_link)
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


def _valid_alias(alias: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", alias.lower()))


async def dot_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle lightweight dot shortcuts like .p bonsai and .alias bonsai on-chain-bonsai."""
    text = (update.message.text or "").strip()
    if not text.startswith("."):
        return

    parts = text.split()
    command = parts[0].lower()
    user_id = update.effective_user.id

    if command in (".p", ".fp", ".floor"):
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format: `.p slug_or_alias`\n"
                "Contoh: `.p azuki`\n"
                "Alias: `.alias bonsai on-chain-bonsai`, lalu `.p bonsai`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        key = parts[1].lower()
        slug = db.get_slug_alias(user_id, key) or key
        await send_floor_overview(update.message, slug)
        return

    if command in (".alias", ".setalias"):
        if len(parts) < 3:
            await update.message.reply_text(
                "❌ Format: `.alias nama_pendek slug_asli`\n"
                "Contoh: `.alias bonsai on-chain-bonsai`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        alias = parts[1].lower()
        slug = parts[2].lower()
        if not _valid_alias(alias):
            await update.message.reply_text(
                "❌ Alias hanya boleh huruf kecil, angka, `_`, atau `-`, maksimal 32 karakter.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await update.message.reply_text(f"🔍 Memverifikasi `{slug}`...", parse_mode=ParseMode.MARKDOWN)
        stats = await opensea_api.get_collection_stats(slug)
        if stats and "error" in stats:
            await update.message.reply_text(f"❌ {stats['error']}")
            return

        if db.set_slug_alias(user_id, alias, slug):
            await update.message.reply_text(
                f"✅ Alias disimpan: `.{alias}` → `{slug}`\n"
                f"Pakai: `.p {alias}`",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("❌ Gagal menyimpan alias.")
        return

    if command in (".aliases", ".aliaslist"):
        aliases = db.get_slug_aliases(user_id)
        if not aliases:
            await update.message.reply_text(
                "📎 Belum ada alias.\nContoh buat: `.alias bonsai on-chain-bonsai`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        msg = "📎 *Alias Slug Anda:*\n\n"
        for alias, slug in aliases:
            msg += f"• `{alias}` → `{slug}`\n"
        msg += "\nPakai: `.p nama_alias`"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    if command in (".delalias", ".unalias"):
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format: `.delalias nama_alias`\nContoh: `.delalias bonsai`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        alias = parts[1].lower()
        if db.remove_slug_alias(user_id, alias):
            await update.message.reply_text(f"✅ Alias `{alias}` dihapus.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"❌ Alias `{alias}` tidak ditemukan.", parse_mode=ParseMode.MARKDOWN)
        return


async def post_init(application: Application) -> None:
    """Set bot commands for Telegram's built-in menu."""
    commands = [
        BotCommand("start", "🏠 Menu utama"),
        BotCommand("help", "📖 Bantuan"),
        BotCommand("floor", "🔍 Cek floor price"),
        BotCommand("track", "📌 Pantau koleksi (bisa banyak)"),
        BotCommand("untrack", "🗑 Hapus pantauan (bisa banyak)"),
        BotCommand("list", "📋 Daftar pantauan"),
        BotCommand("check", "📊 Cek floor watchlist (/check offer utk offer)"),
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


async def _fetch_stats_map(slugs: list[str]) -> dict:
    """Fetch collection stats for many slugs concurrently -> {slug: stats|None}."""
    unique = list(dict.fromkeys(slugs))  # de-dup, preserve order
    if not unique:
        return {}
    results = await asyncio.gather(
        *(opensea_api.get_collection_stats(slug) for slug in unique),
        return_exceptions=True,
    )
    return {
        slug: (res if isinstance(res, dict) else None)
        for slug, res in zip(unique, results)
    }


async def _fetch_offers_map(slugs: list[str]) -> dict:
    """Fetch top collection offers for many slugs concurrently -> {slug: offer|None}."""
    unique = list(dict.fromkeys(slugs))
    if not unique:
        return {}
    results = await asyncio.gather(
        *(opensea_api.get_top_collection_offer(slug) for slug in unique),
        return_exceptions=True,
    )
    return {
        slug: (res if isinstance(res, dict) else None)
        for slug, res in zip(unique, results)
    }


async def _build_check(user_id: int, mode: str = "floor", sort: str = "add"):
    """Fetch tracked prices in parallel for the check view.

    mode: 'floor' | 'offer'. sort: 'add' (insertion order) | 'hi' (price high→low).
    Returns (text, ordered_items) where ordered_items = [(row_id, slug), ...] in the
    same order as the numbered list, so buttons (keyed by stable row_id) line up.
    Returns (None, []) when the watchlist is empty.
    """
    items = db.get_tracked_with_ids(user_id)
    if not items:
        return None, []

    slugs = [slug for _, slug in items]
    fetch = _fetch_offers_map(slugs) if mode == "offer" else _fetch_stats_map(slugs)
    eth_data, price_map = await asyncio.gather(price_api.get_eth_price(), fetch)
    idr_rate = eth_data.get("idr", 0) if eth_data and "error" not in eth_data else 0

    rows = []  # [row_id, slug, status, price, symbol, error]
    for row_id, slug in items:
        data = price_map.get(slug)
        if mode == "offer":
            if data and "error" not in data:
                rows.append([row_id, slug, "ok", data.get("value", 0), data.get("symbol", "WETH"), None])
            else:
                err = data.get("error") if isinstance(data, dict) else None
                rows.append([row_id, slug, "error", None, "WETH", err or "Belum ada offer"])
        else:
            if data and "error" not in data:
                total = data.get("total", {})
                rows.append([row_id, slug, "ok", total.get("floor_price", 0),
                             total.get("floor_price_symbol", "ETH"), None])
            else:
                err = data.get("error") if isinstance(data, dict) else None
                rows.append([row_id, slug, "error", None, "ETH", err or "Gagal mengambil data"])

    if sort == "hi":
        # Priced rows first (highest price first); un-priced/error rows sink to the bottom.
        rows.sort(key=lambda r: (r[2] == "ok", r[3] or 0), reverse=True)

    label = "Offer" if mode == "offer" else "Floor"
    title = "🏷 *Tracked Top Offers*" if mode == "offer" else "📊 *Tracked Floors*"
    if sort == "hi":
        title += " · termahal"
    text = _format_tracked_price_results(
        [(r[1], r[2], r[3], r[4], r[5]) for r in rows],
        idr_rate, title=title, label=label,
    )
    ordered_items = [(r[0], r[1]) for r in rows]
    return text, ordered_items


async def _build_portfolio_text(portfolio: list) -> str:
    """Fetch every holding's floor in parallel and format P/L, ROI, and value."""
    slugs = [row[0] for row in portfolio]
    eth_data, stats_map = await asyncio.gather(
        price_api.get_eth_price(),
        _fetch_stats_map(slugs),
    )
    idr_rate = eth_data.get("idr", 0) if eth_data and "error" not in eth_data else 0

    lines = ["💼 *Portfolio*", f"{len(portfolio)} koleksi", ""]
    total_cost = 0.0
    total_value = 0.0
    for slug, quantity, buy_price, _ in portfolio:
        stats = stats_map.get(slug)
        display = _escape_md_text(_compact_slug(slug))
        if stats and "error" not in stats:
            total_data = stats.get("total", {})
            current_price = total_data.get("floor_price", 0) or 0
            symbol = total_data.get("floor_price_symbol", "ETH")
            item_cost = quantity * buy_price
            item_value = quantity * current_price
            pl = item_value - item_cost
            roi = (pl / item_cost * 100) if item_cost > 0 else 0
            emoji = "🟢" if pl >= 0 else "🔴"
            sign = "+" if pl >= 0 else ""
            lines.append(f"*{display}* · {quantity} NFT")
            lines.append(f"├ Buy `{buy_price:.4f}` → Now `{current_price:.4f}` {symbol}")
            lines.append(f"└ P/L *{sign}{pl:.4f} {symbol}* ({sign}{roi:.1f}%) {emoji}")
            lines.append("")
            total_cost += item_cost
            total_value += item_value
        else:
            lines.append(f"*{display}* · {quantity} NFT")
            lines.append("└ ❌ Gagal ambil harga")
            lines.append("")
            total_cost += quantity * buy_price

    total_pl = total_value - total_cost
    total_roi = (total_pl / total_cost * 100) if total_cost > 0 else 0
    emoji = "🟢" if total_pl >= 0 else "🔴"
    sign = "+" if total_pl >= 0 else ""
    idr_text = f"\n💱 Value ≈ *Rp {total_value * idr_rate:,.0f}*" if idr_rate else ""
    lines.extend([
        "📊 *Summary*",
        f"Cost *{total_cost:.4f}* • Value *{total_value:.4f} ETH*",
        f"P/L *{sign}{total_pl:.4f} ETH* ({sign}{total_roi:.1f}%) {emoji}{idr_text}",
    ])
    return "\n".join(lines).strip()


def _parse_slugs(text: str, limit: int = 20) -> list[str]:
    """Split user text into unique lowercase slugs (space / comma / newline separated)."""
    parts = re.split(r"[\s,]+", text.strip())
    slugs = []
    for p in parts:
        p = p.strip().lower()
        if p and p not in slugs:
            slugs.append(p)
        if len(slugs) >= limit:
            break
    return slugs


def _format_batch_summary(title: str, groups: list) -> str:
    """Format a multi-slug action result. groups: list of (label, [slugs])."""
    lines = [title, ""]
    for label, items in groups:
        if items:
            joined = ", ".join(f"`{_escape_md_text(_compact_slug(s, 22))}`" for s in items)
            lines.append(f"{label} *{len(items)}*: {joined}")
    return "\n".join(lines).strip()


async def _batch_track(user_id: int, slugs: list[str]) -> str:
    """Verify (in parallel) and track many collections at once."""
    stats_map = await _fetch_stats_map(slugs)
    added, exists, failed = [], [], []
    for slug in slugs:
        stats = stats_map.get(slug)
        if not stats or "error" in stats:
            failed.append(slug)
        elif db.add_tracked_collection(user_id, slug):
            added.append(slug)
        else:
            exists.append(slug)
    return _format_batch_summary("📌 *Track*", [
        ("✅ Ditambahkan", added),
        ("ℹ️ Sudah ada", exists),
        ("❌ Tidak ditemukan", failed),
    ])


def _batch_untrack(user_id: int, slugs: list[str]) -> str:
    """Untrack many collections at once (no API calls needed)."""
    removed, notfound = [], []
    for slug in slugs:
        (removed if db.remove_tracked_collection(user_id, slug) else notfound).append(slug)
    return _format_batch_summary("🗑 *Untrack*", [
        ("✅ Dihapus", removed),
        ("❌ Tidak ada di watchlist", notfound),
    ])


def _batch_remove_portfolio(user_id: int, slugs: list[str]) -> str:
    """Remove many portfolio holdings at once."""
    removed, notfound = [], []
    for slug in slugs:
        (removed if db.remove_portfolio_item(user_id, slug) else notfound).append(slug)
    return _format_batch_summary("➖ *Hapus NFT*", [
        ("✅ Dihapus", removed),
        ("❌ Tidak ada di portfolio", notfound),
    ])


async def _build_floor_overview(slug: str):
    """Fetch and format the rich floor/market overview. Returns (text, keyboard).

    On failure, keyboard is None.
    """
    overview_task = opensea_api.get_collection_overview(slug)
    eth_task = price_api.get_eth_price()
    (stats, collection_info, sales_data), eth_data = await asyncio.gather(overview_task, eth_task)

    if stats is None:
        return "❌ Gagal mengambil data. Silakan coba lagi.", None
    if isinstance(stats, dict) and "error" in stats:
        return f"❌ {stats['error']}", None

    text = opensea_api.format_floor_price(
        stats,
        collection_info,
        sales_data=sales_data,
        eth_price=eth_data,
        collection_slug=slug,
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Floor Alert", callback_data=f"qa_alert_{slug}"),
         InlineKeyboardButton("🏷 Offer Alert", callback_data=f"qa_offer_{slug}")],
        [InlineKeyboardButton("📌 Watchlist", callback_data=f"qa_track_{slug}"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    return text, keyboard


async def send_floor_overview(message, collection_slug: str) -> None:
    """Send the rich floor/market overview for a collection slug."""
    slug = collection_slug.lower().strip()
    await message.reply_text(f"🔍 Mencari data untuk `{slug}`...", parse_mode=ParseMode.MARKDOWN)
    text, keyboard = await _build_floor_overview(slug)
    await message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


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
    await send_floor_overview(update.message, collection_slug)


async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add one or more collections to the watchlist."""
    slugs = _parse_slugs(" ".join(context.args))
    if not slugs:
        await update.message.reply_text(
            "❌ Masukkan minimal satu slug (boleh banyak sekaligus).\n"
            "Contoh: `/track azuki` atau `/track azuki pudgypenguins boredapeyachtclub`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_id = update.effective_user.id
    if len(slugs) > 1:
        await update.message.reply_text(f"🔍 Memverifikasi {len(slugs)} koleksi...")

    text = await _batch_track(user_id, slugs)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list"),
         InlineKeyboardButton("📊 Cek Harga", callback_data="cmd_check")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def untrack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one or more collections from the watchlist."""
    slugs = _parse_slugs(" ".join(context.args))
    if not slugs:
        await update.message.reply_text(
            "❌ Masukkan minimal satu slug (boleh banyak sekaligus).\n"
            "Contoh: `/untrack azuki` atau `/untrack azuki pudgypenguins`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_id = update.effective_user.id
    text = _batch_untrack(user_id, slugs)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Watchlist", callback_data="cmd_list"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all tracked collections."""
    user_id = update.effective_user.id
    collections = db.get_tracked_collections(user_id)

    await update.message.reply_text(_format_watchlist(collections), parse_mode=ParseMode.MARKDOWN)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check prices for all tracked collections."""
    user_id = update.effective_user.id
    collections = db.get_tracked_collections(user_id)

    if not collections:
        await update.message.reply_text(
            _format_watchlist(collections),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    args = [a.lower() for a in context.args]
    mode = "offer" if any(a in ("offer", "offers", "bid") for a in args) else "floor"
    sort = "hi" if any(a in ("hi", "high", "termahal", "desc", "mahal") for a in args) else "add"
    await update.message.reply_text("🏷 Mengambil top offer..." if mode == "offer" else "🔍 Mengambil floor price...")

    text, ordered_items = await _build_check(user_id, mode, sort)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=_check_keyboard(ordered_items, mode, sort),
    )


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set price alert for a collection."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Format: `/alert <slug> <harga> [above/below] [floor/offer] [repeat]`\n"
            "Contoh: `/alert boredapeyachtclub 50`\n"
            "        `/alert azuki 15 above repeat`\n"
            "        `/alert azuki 1 above offer`\n\n"
            "Default: below, floor price.",
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
    price_basis = "floor"
    for arg in context.args[2:]:
        low = arg.lower()
        if low in ("above", "below"):
            alert_type = low
        elif low in ("floor", "offer", "top_offer", "topoffer", "top-offer", "bid"):
            price_basis = _normalize_basis(low)

    is_recurring = "repeat" in [a.lower() for a in context.args[2:]]

    user_id = update.effective_user.id

    # Verify collection exists (floor stats works for any valid slug).
    stats = await opensea_api.get_collection_stats(collection_slug)
    if stats and "error" in stats:
        await update.message.reply_text(f"❌ {stats['error']}")
        return

    if price_basis == "top_offer":
        current_price, symbol = await _current_offer_price(collection_slug)
    else:
        total = stats.get("total", {}) if stats else {}
        current_price = total.get("floor_price", 0) or 0
        symbol = total.get("floor_price_symbol", "ETH")

    success = db.add_price_alert(user_id, collection_slug, target_price, alert_type,
                                 is_recurring=is_recurring, current_price=current_price,
                                 price_basis=price_basis)

    if success:
        await update.message.reply_text(
            _format_price_alert_created(
                collection_slug, current_price, symbol, target_price, alert_type,
                is_recurring, price_basis
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Alert ({_basis_label(price_basis)}) untuk `{collection_slug}` "
            f"dengan target {target_price} {symbol} ({alert_type}) sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active alerts for user."""
    user_id = update.effective_user.id

    text, keyboard = _alerts_overview_payload(user_id)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


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

    if success:
        await update.message.reply_text(
            _format_percent_alert_created(
                collection_slug, ref_price, symbol, percentage, direction, is_recurring
            ),
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

    message = opensea_api.format_volume_stats(
        stats, collection_info, previous_volume, collection_slug=collection_slug
    )
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
            _format_volume_alert_created(collection_slug, multiplier),
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
        await update.message.reply_text(
            _format_portfolio_item_added(collection_slug, quantity, buy_price),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("❌ Gagal menambahkan ke portofolio.")


async def removenft_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one or more holdings from the portfolio."""
    slugs = _parse_slugs(" ".join(context.args))
    if not slugs:
        await update.message.reply_text(
            "❌ Format: `/removenft <slug> [slug2 ...]`\n"
            "Contoh: `/removenft azuki` atau `/removenft azuki pudgypenguins`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_id = update.effective_user.id
    text = _batch_remove_portfolio(user_id, slugs)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Portofolio", callback_data="cmd_portfolio"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user portfolio with P/L and ROI."""
    user_id = update.effective_user.id
    portfolio = db.get_portfolio(user_id)

    if not portfolio:
        await update.message.reply_text(
            _format_empty_state(
                "💼 *Portfolio*",
                "Belum ada posisi NFT yang tersimpan.",
                "Tambah posisi dengan `/addnft slug jumlah buy_price`."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await update.message.reply_text("💼 Menghitung portofolio Anda...")

    text = await _build_portfolio_text(portfolio)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="cmd_portfolio"),
         InlineKeyboardButton("➕ Tambah NFT", callback_data="cmd_addnft")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

# ============== ETH Price & Converter Commands ==============

async def ethprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check current ETH price in USD and IDR."""
    await update.message.reply_text("💱 Mengambil harga ETH...")

    eth_data = await price_api.get_eth_price()
    message = price_api.format_eth_price(eth_data)
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert ETH amount to USD and IDR."""
    amount = _parse_eth_amount(" ".join(context.args)) if context.args else None
    if amount is None:
        await update.message.reply_text(
            "❌ Format: `/convert <jumlah_eth>`\n"
            "Contoh: `/convert 0.5`\n\n"
            "_Tip: kirim angka saja (mis._ `0.5`_) tanpa perintah juga bisa._",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    eth_data = await price_api.get_eth_price()
    if eth_data is None or "error" in eth_data:
        error_msg = eth_data.get("error", "Gagal mengambil data") if eth_data else "Gagal mengambil data"
        await update.message.reply_text(f"❌ {error_msg}")
        return

    await update.message.reply_text(
        _format_eth_conversion(amount, eth_data),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_convert_result_keyboard(),
    )


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

    if success:
        await update.message.reply_text(
            _format_gas_alert_created(target_gwei, alert_type),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Gas alert dengan setting ini sudah ada.",
            parse_mode=ParseMode.MARKDOWN
        )


async def _fetch_alert_price(slug: str, basis: str):
    """Fetch (price, symbol) for one (slug, basis). Returns None on error/missing."""
    try:
        if basis == "top_offer":
            offer = await opensea_api.get_top_collection_offer(slug)
            if not offer or "error" in offer:
                return None
            return offer.get("value"), offer.get("symbol", "WETH")
        stats = await opensea_api.get_collection_stats(slug)
        if not stats or "error" in stats:
            return None
        total = stats.get("total", {})
        floor = total.get("floor_price")
        if floor is None:
            return None
        return floor, total.get("floor_price_symbol", "ETH")
    except Exception as e:
        logger.error(f"Alert price fetch failed for {slug}/{basis}: {e}")
        return None


async def _fetch_alert_prices(keys, concurrency: int = 10) -> dict:
    """Fetch prices for many (slug, basis) keys concurrently (deduped, capped).

    Returns {(slug, basis): (price, symbol) | None}. This is what keeps the
    background alert cycle fast — one fetch per unique collection, in parallel,
    instead of one sequential request per alert.
    """
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}
    sem = asyncio.Semaphore(concurrency)

    async def one(slug, basis):
        async with sem:
            return await _fetch_alert_price(slug, basis)

    results = await asyncio.gather(*(one(slug, basis) for slug, basis in unique))
    return dict(zip(unique, results))


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check price alerts."""
    alerts = db.get_all_active_alerts()
    if not alerts:
        return

    # Fetch every distinct (collection, basis) once, in parallel.
    price_map = await _fetch_alert_prices((a[1], a[7]) for a in alerts)

    for (user_id, collection_slug, target_price, alert_type, is_recurring,
         last_price, triggered_at, price_basis) in alerts:
        try:
            data = price_map.get((collection_slug, price_basis))
            if not data:
                continue
            current_price, symbol = data
            basis_label = "Top Offer" if price_basis == "top_offer" else "Floor Price"

            condition_met = _price_alert_condition_met(alert_type, current_price, target_price)
            crossed = _price_alert_crossed(alert_type, last_price, target_price)
            should_trigger = condition_met and (not is_recurring or not triggered_at or crossed)

            if should_trigger:
                message = (
                    f"🚨 *Alert Triggered!*\n\n"
                    f"Koleksi: `{collection_slug}`\n"
                    f"{basis_label}: *{current_price:.4f} {symbol}*\n"
                    f"Target: {alert_type} {target_price} {symbol}"
                )

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    db.deactivate_alert(
                        user_id,
                        collection_slug,
                        target_price,
                        alert_type,
                        current_price=current_price,
                        price_basis=price_basis,
                    )
                except Exception as e:
                    logger.error(f"Failed to send alert to user {user_id}: {e}")
            else:
                db.update_price_alert_observed_price(
                    user_id, collection_slug, target_price, alert_type, current_price,
                    price_basis=price_basis
                )

        except Exception as e:
            logger.error(f"Error checking alert for {collection_slug}: {e}")


async def check_percentage_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check percentage-based alerts."""
    alerts = db.get_all_percentage_alerts()
    if not alerts:
        return

    # Percentage alerts all track floor; fetch each collection's floor once.
    price_map = await _fetch_alert_prices((a[1], "floor") for a in alerts)

    for user_id, collection_slug, percentage, direction, reference_price, _is_recurring in alerts:
        try:
            data = price_map.get((collection_slug, "floor"))
            if not data:
                continue
            current_price, symbol = data

            ref_price = reference_price or db.get_oldest_price(collection_slug)
            if not ref_price or ref_price <= 0:
                db.update_percentage_alert_ref_price(
                    user_id, collection_slug, percentage, direction, current_price
                )
                continue

            change_pct = ((current_price - ref_price) / ref_price) * 100

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
                    f"🚨 *Percentage Alert!*\n\n"
                    f"Koleksi: `{collection_slug}`\n"
                    f"Perubahan: {trend} *{sign}{change_pct:.1f}%*\n"
                    f"Harga referensi: {ref_price:.4f} {symbol}\n"
                    f"Harga sekarang: *{current_price:.4f} {symbol}*"
                )

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    db.deactivate_percentage_alert(
                        user_id,
                        collection_slug,
                        percentage,
                        direction,
                        new_ref_price=current_price,
                    )
                except Exception as e:
                    logger.error(f"Failed to send percentage alert to user {user_id}: {e}")

        except Exception as e:
            logger.error(f"Error checking percentage alert for {collection_slug}: {e}")


async def check_volume_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to check volume spike alerts."""
    alerts = db.get_all_volume_alerts()

    for user_id, collection_slug, multiplier, last_triggered_at in alerts:
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

                    if spike_ratio >= multiplier and not _is_in_cooldown(
                        last_triggered_at, VOLUME_ALERT_COOLDOWN_SECONDS
                    ):
                        symbol = stats.get("total", {}).get("floor_price_symbol", "ETH")

                        message = (
                            f"🚨 *Volume Spike Alert!*\n\n"
                            f"Koleksi: `{collection_slug}`\n"
                            f"Volume 24h: *{current_volume:.2f} {symbol}*\n"
                            f"Rata-rata: {avg_volume:.2f} {symbol}\n"
                            f"Spike: *{spike_ratio:.1f}x* 📊"
                        )

                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=message,
                                parse_mode=ParseMode.MARKDOWN
                            )
                            db.mark_volume_alert_triggered(user_id, collection_slug)
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
                    f"⛽ *Gas Alert!*\n\n"
                    f"Gas saat ini: *{current_gas:.1f} gwei*\n"
                    f"Target: {type_text} {target_gwei} gwei ✅"
                )

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    db.deactivate_gas_alert(user_id, target_gwei, alert_type)
                except Exception as e:
                    logger.error(f"Failed to send gas alert to user {user_id}: {e}")


async def record_price_history(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job to record price history for all monitored collections."""
    collections = set(db.get_all_monitored_collection_slugs())

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
        msg = _format_mint_added(nft_name, mint_price, date_str, mint_link)
    else:
        msg = "❌ Gagal menambahkan reminder."

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def mints_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active mint reminders."""
    user_id = update.effective_user.id
    reminders = db.get_mint_reminders(user_id)

    if not reminders:
        await update.message.reply_text(
            _format_mint_reminders(reminders),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    message = _format_mint_reminders(reminders)
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
                time_text = "⏰ *30 menit lagi!*"

            # 5 minute reminder
            elif not reminded_5 and 0 <= minutes_until <= 8:
                should_send = True
                reminder_type = "5min"
                time_text = "🚨 *5 menit lagi!*"

            if should_send:
                message = (
                    f"🗓 *Mint Reminder!*\n\n"
                    f"{time_text}\n\n"
                    f"NFT: *{nft_name}*\n"
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

    # Add lightweight dot command handler, e.g. ".p azuki" or ".p myalias"
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\."), dot_command_handler
    ))

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
