"""
StudyBot — Fully Advanced Telegram Bot (Single File)
Features:
- Classes 9-12, Categories, Subjects, Chapters, PDFs
- Admin upload via caption: class|category|subject|chapter|title|premium
- Quizzes: chapter-wise & random
- Premium: UPI + Razorpay + auto upgrade + expiry
- SQLite async DB
- Pagination & batch PDF send
- Admin stats & premium activation
"""

import os
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Poll, ChatAction, Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Optional Razorpay
try:
    import razorpay
    RAZORPAY_AVAILABLE = True
except:
    RAZORPAY_AVAILABLE = False

# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x)

PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "priyansh563@ybl")
PAYMENT_NOTE = os.getenv("PAYMENT_NOTE", "StudyBot Premium")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
PORT = int(os.getenv("PORT","8080"))

CLASSES = ["9","10","11","12"]
CATEGORIES = ["Short Notes","PYQ","Sample Papers","Handwritten Notes","Test Series","Quizzes"]
PAGE_SIZE = 8
DB_PATH = "studybot.db"

PLANS = {
    "1m":{"months":1,"amount":9900,"label":"1 Month ₹99"},
    "3m":{"months":3,"amount":24900,"label":"3 Months ₹249"},
    "12m":{"months":12,"amount":69900,"label":"12 Months ₹699"},
}

PRICE_TEXT = (
    "\n".join([
        "⭐ *Premium Plans*",
        "• 1 Month: ₹99",
        "• 3 Months: ₹249",
        "• 12 Months: ₹699",
        "",
        "Premium unlocks Test Series, Exclusive Notes, Sample Papers & Fast Support.",
        "",
        f"1) UPI: `{PAYMENT_UPI_ID}`",
        f"2) Note me likhein: `{PAYMENT_NOTE}`",
        "3) TXN ID bhejein: /redeem <TXN_ID>",
        "4) Admin verify karenge aur premium activate ho jayega.",
    ])
)

# -------------------- DB --------------------
async def get_db():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with get_db() as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            name TEXT,
            is_premium INTEGER DEFAULT 0,
            premium_expiry TEXT,
            joined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_num TEXT,
            category TEXT,
            subject TEXT,
            chapter TEXT,
            title TEXT,
            file_id TEXT,
            premium INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_num TEXT,
            subject TEXT,
            chapter TEXT,
            question TEXT,
            option1 TEXT,
            option2 TEXT,
            option3 TEXT,
            option4 TEXT,
            correct_index INTEGER,
            premium INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            txn_id TEXT,
            plan TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            quiz_id INTEGER,
            chosen_index INTEGER,
            correct INTEGER,
            timestamp TEXT
        );
        """)
        await db.commit()

async def add_user(tg_id:int,name:str):
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (tg_id,name,joined_at) VALUES (?,?,?)",
            (tg_id,name,datetime.utcnow().isoformat())
        )
        await db.commit()

async def is_premium(tg_id:int) -> bool:
    async with get_db() as db:
        cursor = await db.execute("SELECT is_premium,premium_expiry FROM users WHERE tg_id=?",(tg_id,))
        row = await cursor.fetchone()
        if not row: return False
        if row[1]:
            expiry = datetime.fromisoformat(row[1])
            if expiry < datetime.utcnow():
                await db.execute("UPDATE users SET is_premium=0 WHERE tg_id=?",(tg_id,))
                await db.commit()
                return False
        return bool(row[0])

async def upgrade_premium(tg_id:int,months:int):
    async with get_db() as db:
        cursor = await db.execute("SELECT premium_expiry FROM users WHERE tg_id=?",(tg_id,))
        row = await cursor.fetchone()
        now = datetime.utcnow()
        start = max(now, datetime.fromisoformat(row[0])) if row and row[0] else now
        new_expiry = start + timedelta(days=30*months)
        await db.execute("UPDATE users SET is_premium=1,premium_expiry=? WHERE tg_id=?",(new_expiry.isoformat(),tg_id))
        await db.commit()

# -------------------- HELPERS --------------------
def is_admin(user_id:int):
    return user_id in ADMIN_IDS

def parse_caption(caption:str):
    try:
        parts = [p.strip() for p in caption.split("|")]
        if len(parts)!=6: return None
        class_num,category,subject,chapter,title,premium = parts
        prem = 1 if premium.lower() in {"1","true","yes","y"} else 0
        return class_num,category,subject,chapter,title,prem
    except: return None

# -------------------- MENU HANDLERS --------------------
async def send_menu(update:Update,ctx):
    kb = [[InlineKeyboardButton(f"Class {c}",callback_data=f"class|{c}")] for c in CLASSES]
    kb.append([InlineKeyboardButton("Buy Premium ⭐",callback_data="buy")])
    await update.effective_message.reply_text("📚 Choose your class:",reply_markup=InlineKeyboardMarkup(kb))

async def send_categories(query,class_num):
    kb = [[InlineKeyboardButton(cat,callback_data=f"cat|{class_num}|{cat}")] for cat in CATEGORIES]
    kb.append([InlineKeyboardButton("⬅️ Back",callback_data="home")])
    await query.edit_message_text(f"Class {class_num} → Choose category:",reply_markup=InlineKeyboardMarkup(kb))

async def list_subjects(class_num:str,category:str):
    async with get_db() as db:
        cursor = await db.execute("SELECT DISTINCT subject FROM content WHERE class_num=? AND category=? ORDER BY subject",(class_num,category))
        return [r[0] for r in await cursor.fetchall()]

async def send_subjects(query,class_num,category):
    subs = await list_subjects(class_num,category)
    if not subs: subs=["Maths","Physics","Chemistry","Biology","English","Hindi","SST"]
    kb = [[InlineKeyboardButton(s,callback_data=f"sub|{class_num}|{category}|{s}")] for s in subs]
    kb.append([InlineKeyboardButton("⬅️ Back",callback_data=f"class|{class_num}")])
    await query.edit_message_text(f"Class {class_num} → {category} → Choose subject:",reply_markup=InlineKeyboardMarkup(kb))

async def list_chapters(class_num,category,subject):
    async with get_db() as db:
        cursor = await db.execute("SELECT DISTINCT chapter FROM content WHERE class_num=? AND category=? AND subject=? ORDER BY chapter",(class_num,category,subject))
        return [r[0] for r in await cursor.fetchall()]

async def send_chapters(query,class_num,category,subject):
    chs = await list_chapters(class_num,category,subject)
    if not chs: chs=["Chapter 1","Chapter 2","Chapter 3"]
    kb = [[InlineKeyboardButton(ch,callback_data=f"chap|{class_num}|{category}|{subject}|{ch}")] for ch in chs]
    kb.append([InlineKeyboardButton("⬅️ Back",callback_data=f"cat|{class_num}|{category}")])
    await query.edit_message_text(f"Class {class_num} → {category} → {subject} → Choose chapter:",reply_markup=InlineKeyboardMarkup(kb))

async def fetch_items(class_num,category,subject,chapter):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM content WHERE class_num=? AND category=? AND subject=? AND chapter=? ORDER BY created_at DESC",(class_num,category,subject,chapter))
        return await cursor.fetchall()

async def send_items(query,tg_id,class_num,category,subject,chapter,page=0):
    items = await fetch_items(class_num,category,subject,chapter)
    premium_user = await is_premium(tg_id)
    start = page*PAGE_SIZE
    page_items = items[start:start+PAGE_SIZE]
    text = f"Class {class_num} → {category} → {subject} → {chapter}\n\n"
    if not page_items: text+="No items yet."
    else:
        for i,r in enumerate(page_items,1):
            lock = "🔓" if (premium_user or r["premium"]==0) else "🔒 Premium"
            text+=f"{i+start}. {r['title']} {lock}\n"
    buttons=[]
    nav=[]
    if start>0: nav.append(InlineKeyboardButton("⬅️ Prev",callback_data=f"page|{class_num}|{category}|{subject}|{chapter}|{page-1}"))
    if start+PAGE_SIZE<len(items): nav.append(InlineKeyboardButton("Next ➡️",callback_data=f"page|{class_num}|{category}|{subject}|{chapter}|{page+1}"))
    if nav: buttons.append(nav)
    if page_items: buttons.append([InlineKeyboardButton(f"📥 Send #1–#{len(page_items)}",callback_data=f"sendrange|{class_num}|{category}|{subject}|{chapter}|{start}|{len(page_items)}")])
    buttons.append([InlineKeyboardButton("⬅️ Back",callback_data=f"chap|{class_num}|{category}|{subject}")])
    if not premium_user: buttons.append([InlineKeyboardButton("⭐ Buy Premium",callback_data="buy")])
    await query.edit_message_text(text,reply_markup=InlineKeyboardMarkup(buttons))

async def send_documents_by_range(message,tg_id,class_num,category,subject,chapter,start,count):
    items = await fetch_items(class_num,category,subject,chapter)
    premium_user = await is_premium(tg_id)
    subset = items[start:start+count]
    for r in subset:
        if r["premium"] and not premium_user:
            await message.reply_text(f"🔒 {r['title']} — Premium only. Use /buy to unlock.")
            continue
        try:
            await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            await message.reply_document(r["file_id"],caption=f"{r['title']}\n(Class {class_num} • {category} • {subject} • {chapter})")
        except Exception as e:
            await message.reply_text(f"Failed sending: {r['title']} — {e}")

# -------------------- PREMIUM --------------------
async def buy_cmd(update,ctx):
    if RAZORPAY_AVAILABLE and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        kb=[[InlineKeyboardButton(PLANS[k]["label"],callback_data=f"rzp|{k}")] for k in PLANS.keys()]
        kb.append([InlineKeyboardButton("I already paid • Redeem",callback_data="redeem")])
        await update.message.reply_text("Choose Premium plan:",reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_markdown(PRICE_TEXT)

async def redeem_cmd(update,ctx):
    parts = (update.message.text or "").strip().split(maxsplit=1)
    if len(parts)<2:
        await update.message.reply_text("Usage: /redeem <TXN_ID>")
        return
    txn=parts[1].strip()
    async with get_db() as db:
        await db.execute("INSERT INTO purchases (tg_id,txn_id,plan,created_at) VALUES (?,?,?,?)",(update.effective_user.id,txn,"manual-upi",datetime.utcnow().isoformat()))
        await db.commit()
    await update.message.reply_text("Thanks! ✅ TXN ID received. Admin verify karega.")
    for aid in ADMIN_IDS:
        try: await ctx.bot.send_message(aid,f"Redeem request from {update.effective_user.full_name} (#{update.effective_user.id})\nTXN: {txn}")
        except: pass

async def make_premium_cmd(update,ctx):
    if not update.effective_user or not is_admin(update.effective_user.id): return
    parts = (update.message.text or "").split()
    if len(parts)<2:
        await update.message.reply_text("Usage: /make_premium <tg_id>")
        return