"""
Academy to'lov boti — talaba chek yuboradi, AI o'qiydi, Notion'ga yoziladi.

Oqim:
  /start -> ism (bir marta) -> "To'lov qilish" tugmasi -> faol karta
  -> talaba chek yuboradi -> AI o'qiydi -> tekshiruvlar -> Notion
  -> talabaga tasdiq, shubhali bo'lsa adminga xabar

Karta xabarlari chek kelganda yoki 2 soatdan keyin o'chiriladi.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta

import requests
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# Sozlamalar
# ----------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

DB_STUDENTS = os.environ["NOTION_STUDENTS_DB"]
DB_PAYMENTS = os.environ["NOTION_PAYMENTS_DB"]
DB_CARDS = os.environ["NOTION_CARDS_DB"]

ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

TZ = timezone(timedelta(hours=5))
MAX_CHEK_YOSHI = 30          # kun — bundan eski chek shubhali
KARTA_MUDDATI = 2 * 60 * 60  # soniya — karta xabari shuncha turadi

BTN_TOLOV = "💳 To'lov qilish"
BTN_TOLOVLARIM = "📋 Mening to'lovlarim"

MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_TOLOV)], [KeyboardButton(BTN_TOLOVLARIM)]],
    resize_keyboard=True,
)

RAQAMLAR = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("tolov-bot")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

student_cache: dict[int, dict] = {}
write_lock = asyncio.Lock()
karta_navbat = {"n": 0}  # bir nechta faol karta bo'lsa — navbat bilan ko'rsatish


# ----------------------------------------------------------------------------
# Yordamchilar
# ----------------------------------------------------------------------------

def money(value) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def sana_matn(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10]


def oxirgi4(karta: str | None) -> str:
    raqamlar = re.sub(r"\D", "", karta or "")
    return raqamlar[-4:] if len(raqamlar) >= 4 else ""


def txt(prop: dict) -> str:
    return "".join(p.get("plain_text", "") for p in (prop.get("rich_text") or []))


def title_txt(prop: dict) -> str:
    return "".join(p.get("plain_text", "") for p in (prop.get("title") or []))


def rt(value):
    return {"rich_text": [{"text": {"content": str(value)[:1900]}}] if value else []}


# ----------------------------------------------------------------------------
# AI
# ----------------------------------------------------------------------------

SYSTEM_PROMPT = """You read payment receipts from Uzbekistan (bank apps, payment
services, PDF receipts). Receipts come from many different banks and apps, in
Uzbek, Russian or English, in any layout.

Work in two steps and output both.

STEP 1 — Read everything. Inside <transkript></transkript>, transcribe every
piece of text you can see, line by line, exactly as written, including labels,
amounts, names, card numbers, button captions, status text and timestamps.

STEP 2 — Extract. Inside <json></json>, output one JSON object with these keys:

{
  "chek_emas": boolean,
  "holat": string,            // "muvaffaqiyatli" | "tasdiqlanmagan" | "muvaffaqiyatsiz"
  "summa": number|null,       // amount that reached the RECIPIENT
  "komissiya": number|null,
  "bank": string|null,
  "sana": string|null,        // "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD"
  "yuboruvchi": string|null,
  "qabul_kartasi": string|null,
  "qabul_ism": string|null,
  "tranzaksiya_id": string|null,
  "ishonch": number,          // 0.0-1.0
  "izoh": string|null         // short note in Uzbek if something is unclear
}

Rules:
- "summa" is what the recipient receives. If both an amount and a larger "with
  commission" / "total debited" figure are shown, take the smaller one and put
  the difference in "komissiya".
- Amounts are plain numbers: "450 000,00 UZS" -> 450000.
- If the screen is a pre-transfer confirmation form (a button like "O'tkazish",
  "Перевести", "Confirm" still waiting to be pressed), set "holat" to
  "tasdiqlanmagan".
- Missing field -> null. Never invent, never guess a year that is not shown.
- "ishonch" below 0.8 means a human should check it.
- Output nothing outside the two tags."""


def call_claude(file_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(file_bytes).decode()
    block_type = "document" if media_type == "application/pdf" else "image"

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": block_type,
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": "Read this receipt."},
                    ],
                }
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))

    match = re.search(r"<json>(.*?)</json>", text, re.S)
    if not match:
        raise ValueError(f"JSON topilmadi: {text[:300]}")

    data = json.loads(match.group(1).strip())
    tr = re.search(r"<transkript>(.*?)</transkript>", text, re.S)
    data["_transkript"] = tr.group(1).strip() if tr else ""
    return data


# ----------------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------------

def nq(db_id: str, filter_=None, sorts=None, limit=100) -> list:
    body: dict = {"page_size": min(limit, 100)}
    if filter_:
        body["filter"] = filter_
    if sorts:
        body["sorts"] = sorts
    r = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers=NOTION_HEADERS,
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["results"]


def ncreate(db_id: str, props: dict) -> dict:
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={
            "parent": {"type": "database_id", "database_id": db_id},
            "properties": props,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def nupdate(page_id: str, props: dict) -> dict:
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": props},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def upload_file(file_bytes: bytes, filename: str, content_type: str) -> str | None:
    try:
        r = requests.post(
            "https://api.notion.com/v1/file_uploads",
            headers=NOTION_HEADERS,
            json={"filename": filename, "content_type": content_type},
            timeout=30,
        )
        r.raise_for_status()
        info = r.json()
        url = info.get("upload_url") or (
            f"https://api.notion.com/v1/file_uploads/{info['id']}/send"
        )
        r2 = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
            },
            files={"file": (filename, file_bytes, content_type)},
            timeout=120,
        )
        r2.raise_for_status()
        return info["id"]
    except Exception as exc:
        log.warning("Fayl yuklanmadi: %s", exc)
        return None


def get_student(tg_id: int) -> dict | None:
    if tg_id in student_cache:
        return student_cache[tg_id]
    rows = nq(
        DB_STUDENTS,
        {"property": "Telegram ID", "rich_text": {"equals": str(tg_id)}},
        limit=1,
    )
    if not rows:
        return None
    info = {
        "page_id": rows[0]["id"],
        "ism": title_txt(rows[0]["properties"].get("Ism", {})),
    }
    student_cache[tg_id] = info
    return info


def create_student(tg_id: int, ism: str, username: str, profil: str) -> dict:
    page = ncreate(
        DB_STUDENTS,
        {
            "Ism": {"title": [{"text": {"content": ism[:200]}}]},
            "Telegram ID": rt(tg_id),
            "Username": rt(f"@{username}" if username else ""),
            "Profil nomi": rt(profil),
        },
    )
    info = {"page_id": page["id"], "ism": ism}
    student_cache[tg_id] = info
    return info


def get_cards() -> dict:
    """Kartalarni har safar Notion'dan o'qiydi — kesh yo'q, o'zgarish darhol ko'rinadi."""
    faol, eski = [], []
    for row in nq(DB_CARDS, limit=50):
        p = row["properties"]
        karta = {
            "page_id": row["id"],
            "raqam": title_txt(p.get("Karta raqami", {})),
            "egasi": txt(p.get("Egasi", {})),
            "bank": txt(p.get("Bank", {})),
        }
        if not karta["raqam"]:
            continue
        holat = (p.get("Holati", {}).get("select") or {}).get("name")
        if holat == "Faol":
            faol.append(karta)
        elif holat == "Eski":
            eski.append(karta)

    # tartib doimiy bo'lishi uchun — navbat to'g'ri aylanishi kerak
    faol.sort(key=lambda k: k["raqam"])
    return {"faol": faol, "eski": eski}


def karta_topish(cards: dict, qabul_raqam: str | None) -> dict | None:
    """Chekdagi qabul kartasi bazadagi qaysi kartaga mos kelishini topadi."""
    oxiri = oxirgi4(qabul_raqam)
    if not oxiri:
        return None
    for karta in cards.get("faol", []) + cards.get("eski", []):
        if oxirgi4(karta["raqam"]) == oxiri:
            return karta
    return None


def find_duplicate(file_hash: str, tranzaksiya_id: str | None) -> dict | None:
    rows = nq(
        DB_PAYMENTS, {"property": "Fayl izi", "rich_text": {"equals": file_hash}}, limit=1
    )
    if rows:
        return rows[0]

    tid = (tranzaksiya_id or "").strip()
    if tid:
        rows = nq(
            DB_PAYMENTS,
            {"property": "Tranzaksiya ID", "rich_text": {"equals": tid}},
            limit=1,
        )
        if rows:
            return rows[0]
    return None


def bir_kunda_takror(tg_id: int, summa, sana: str | None) -> dict | None:
    """Shu talabadan shu kuni aynan shu summa allaqachon kelganmi?"""
    if summa is None or not sana:
        return None
    rows = nq(
        DB_PAYMENTS,
        {
            "and": [
                {"property": "Telegram ID", "rich_text": {"equals": str(tg_id)}},
                {"property": "Summa", "number": {"equals": float(summa)}},
                {"property": "Sana", "date": {"equals": sana[:10]}},
            ]
        },
        limit=1,
    )
    return rows[0] if rows else None


def izoh_qoshish(page_id: str, eski_izoh: str, qoshimcha: str) -> None:
    matn = f"{eski_izoh}; {qoshimcha}" if eski_izoh else qoshimcha
    nupdate(page_id, {"Izoh": rt(matn)})


# ----------------------------------------------------------------------------
# Tekshiruvlar
# ----------------------------------------------------------------------------

def tekshir(data: dict, tg_id: int) -> tuple[list[str], dict | None, dict | None]:
    """(muammolar ro'yxati, bir kundagi takror yozuv, topilgan karta)"""
    muammolar = []
    cards = get_cards()

    if data.get("holat") != "muvaffaqiyatli":
        muammolar.append("to'lov tasdiqlanmagan yoki muvaffaqiyatsiz")

    if (data.get("ishonch") or 0) < 0.8:
        muammolar.append("chek aniq o'qilmadi")

    if data.get("summa") is None:
        muammolar.append("summa topilmadi")

    sana = data.get("sana")
    if not sana:
        muammolar.append("chekda sana yo'q")
    else:
        try:
            d = datetime.fromisoformat(sana.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=TZ)
            yosh = (datetime.now(TZ) - d).days
            if yosh > MAX_CHEK_YOSHI:
                muammolar.append(f"chek {yosh} kun oldingi")
        except ValueError:
            muammolar.append("sana noto'g'ri formatda")

    karta = karta_topish(cards, data.get("qabul_kartasi"))
    qabul = oxirgi4(data.get("qabul_kartasi"))
    if qabul:
        if not karta:
            muammolar.append(f"boshqa kartaga tushgan (...{qabul})")
        elif karta not in cards.get("faol", []):
            muammolar.append("eski kartaga to'langan")

    takror = bir_kunda_takror(tg_id, data.get("summa"), sana)
    if takror:
        muammolar.append("shu talabadan bugun aynan shu summa allaqachon kelgan")

    return muammolar, takror, karta


# ----------------------------------------------------------------------------
# Karta xabarlarini boshqarish
# ----------------------------------------------------------------------------

async def kartani_tozalash(context, chat_id: int, user_data: dict, taskni_bekor=True):
    for mid in user_data.pop("karta_msg_ids", []):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass

    task = user_data.pop("karta_task", None)
    if task and taskni_bekor:
        task.cancel()

    user_data.pop("chek_kutilmoqda", None)


async def karta_taymeri(context, chat_id: int, user_data: dict):
    try:
        await asyncio.sleep(KARTA_MUDDATI)
    except asyncio.CancelledError:
        return
    await kartani_tozalash(context, chat_id, user_data, taskni_bekor=False)


# ----------------------------------------------------------------------------
# Handlerlar
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    student = await asyncio.to_thread(get_student, user.id)

    if student:
        await update.message.reply_text(
            f"👋 Assalomu alaykum, {student['ism']}!\n\n"
            f"To'lov qilish uchun pastdagi tugmadan foydalaning. 👇",
            reply_markup=MENU,
        )
        return

    context.user_data["kutilmoqda"] = "ism"
    await update.message.reply_text(
        "👋 Assalomu alaykum!\n\n"
        "Ro'yxatdan o'tish uchun ism va familiyangizni yozing.\n\n"
        "✍️ Masalan: Aziza Karimova"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    matn = (update.message.text or "").strip()

    if matn == BTN_TOLOV:
        await show_card(update, context)
        return

    if matn == BTN_TOLOVLARIM:
        await show_payments(update, context)
        return

    if context.user_data.get("kutilmoqda") == "ism":
        if (
            len(matn) < 3
            or len(matn) > 60
            or not re.search(r"[A-Za-zА-Яа-яЎўҚқҒғҲҳ]{2}", matn)
        ):
            await update.message.reply_text(
                "✍️ Iltimos, ism va familiyangizni to'liq yozing.\n\n"
                "Masalan: Aziza Karimova"
            )
            return

        try:
            student = await asyncio.to_thread(
                create_student,
                user.id,
                matn,
                user.username or "",
                " ".join(filter(None, [user.first_name, user.last_name])),
            )
        except Exception:
            log.exception("Ro'yxatga yozishda xato")
            await update.message.reply_text(
                "⚠️ Xatolik yuz berdi. Iltimos, birozdan keyin qaytadan urinib ko'ring."
            )
            return

        context.user_data.pop("kutilmoqda", None)
        await update.message.reply_text(
            f"✅ Rahmat, {student['ism']}!\n\n"
            f"Ro'yxatdan o'tdingiz. To'lov qilish uchun pastdagi tugmadan "
            f"foydalaning. 👇",
            reply_markup=MENU,
        )
        return

    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text(
            "✍️ Avval ism va familiyangizni yozing.\n\nMasalan: Aziza Karimova"
        )
        return

    await update.message.reply_text(
        "👇 To'lov qilish uchun pastdagi tugmadan foydalaning.", reply_markup=MENU
    )


async def show_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text(
            "✍️ Avval ism va familiyangizni yozing.\n\nMasalan: Aziza Karimova"
        )
        return

    # oldingi karta xabarlari bo'lsa — o'chiramiz
    await kartani_tozalash(context, chat_id, context.user_data)

    try:
        cards = await asyncio.to_thread(get_cards)
    except Exception:
        log.exception("Kartalarni o'qishda xato")
        await update.message.reply_text(
            "⚠️ Xatolik yuz berdi. Iltimos, birozdan keyin qaytadan urinib ko'ring."
        )
        return

    faol_kartalar = cards.get("faol", [])
    if not faol_kartalar:
        log.error("Kartalar bazasida 'Faol' karta yo'q!")
        await update.message.reply_text(
            "⚠️ Hozircha to'lov kartasi mavjud emas.\n"
            "Iltimos, administratorga murojaat qiling."
        )
        return

    # navbat bilan: har safar keyingi faol karta
    faol = faol_kartalar[karta_navbat["n"] % len(faol_kartalar)]
    karta_navbat["n"] += 1

    satrlar = ["💳 To'lov kartasi", ""]
    if faol["egasi"]:
        satrlar.append(f"👤 Karta egasi: {faol['egasi']}")
    if faol["bank"]:
        satrlar.append(f"🏦 Bank: {faol['bank']}")
    satrlar += ["", "👇 Raqamni bosib nusxalang"]

    m1 = await update.message.reply_text("\n".join(satrlar))
    m2 = await update.message.reply_text(
        f"`{faol['raqam']}`", parse_mode="MarkdownV2"
    )
    m3 = await update.message.reply_text(
        "📄 To'lovni amalga oshirgach, chekni shu yerga yuboring.\n\n"
        "✅ Eng yaxshisi — bank ilovasidan chekni PDF qilib yuklab, "
        "shu faylni yuborish. Bunda ma'lumotlar aniq o'qiladi.\n"
        "📸 Imkoni bo'lmasa, screenshot ham bo'ladi."
    )

    context.user_data["karta_msg_ids"] = [m1.message_id, m2.message_id, m3.message_id]
    context.user_data["chek_kutilmoqda"] = True
    context.user_data["karta_task"] = asyncio.create_task(
        karta_taymeri(context, chat_id, context.user_data)
    )


async def show_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text(
            "✍️ Avval ism va familiyangizni yozing.\n\nMasalan: Aziza Karimova"
        )
        return

    try:
        rows = await asyncio.to_thread(
            nq,
            DB_PAYMENTS,
            {"property": "Telegram ID", "rich_text": {"equals": str(user.id)}},
            [{"property": "Sana", "direction": "ascending"}],
            100,
        )
    except Exception:
        log.exception("To'lovlarni o'qishda xato")
        await update.message.reply_text("⚠️ Xatolik yuz berdi. Keyinroq urinib ko'ring.")
        return

    if not rows:
        await update.message.reply_text(
            "📋 Hozircha to'lovlaringiz yo'q.", reply_markup=MENU
        )
        return

    satrlar, jami = [], 0.0
    for i, row in enumerate(rows):
        p = row["properties"]
        summa = p.get("Summa", {}).get("number")
        sana = (p.get("Sana", {}).get("date") or {}).get("start")
        jami += summa or 0
        belgi = RAQAMLAR[i] if i < len(RAQAMLAR) else f"{i + 1}."
        satrlar.append(f"{belgi} {money(summa)} so'm — {sana_matn(sana)}")

    await update.message.reply_text(
        "📋 Sizning to'lovlaringiz\n\n"
        + "\n".join(satrlar)
        + f"\n\n💰 Jami: {money(jami)} so'm",
        reply_markup=MENU,
    )


async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    chat_id = update.effective_chat.id

    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await message.reply_text(
            "✍️ Avval ism va familiyangizni yozing.\n\nMasalan: Aziza Karimova"
        )
        return

    if not context.user_data.get("chek_kutilmoqda"):
        await message.reply_text(
            "💳 Avval \"To'lov qilish\" tugmasini bosing, keyin chekni yuboring. 👇",
            reply_markup=MENU,
        )
        return

    # --- fayl ---
    if message.photo:
        tg_file = await context.bot.get_file(message.photo[-1].file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        media_type, filename = "image/jpeg", f"chek_{message.message_id}.jpg"
    else:
        doc = message.document
        mt = (doc.mime_type or "").lower()
        if mt not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
            await message.reply_text("📄 Faqat PDF yoki rasm yuboring.")
            return
        if doc.file_size and doc.file_size > 18 * 1024 * 1024:
            await message.reply_text("📸 Fayl juda katta. Kichikroq rasm yuboring.")
            return
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        media_type = mt
        filename = doc.file_name or f"chek_{message.message_id}"

    kutish = await message.reply_text("⏳ Chekingiz tekshirilmoqda...")

    try:
        data = await asyncio.to_thread(call_claude, file_bytes, media_type)
    except Exception:
        log.exception("AI xatosi")
        await kutish.edit_text(
            "⚠️ Chekni o'qib bo'lmadi.\n\nIltimos, qaytadan yuboring."
        )
        return

    log.info("Transkript (%s): %s", student["ism"], data.get("_transkript", "")[:400])

    if data.get("chek_emas"):
        await kutish.edit_text(
            "❌ Bu to'lov chekiga o'xshamadi\n\n"
            "📄 Iltimos, to'lov chekini yuboring — bank ilovasidan PDF qilib "
            "yuklang yoki screenshot oling."
        )
        return

    file_hash = hashlib.sha256(file_bytes).hexdigest()[:40]

    async with write_lock:
        try:
            duplicate = await asyncio.to_thread(
                find_duplicate, file_hash, data.get("tranzaksiya_id")
            )
        except Exception:
            log.exception("Dublikat tekshiruvida xato")
            duplicate = None

        if duplicate:
            await kutish.edit_text(
                "♻️ Bu chek avval yuborilgan\n\nYangi to'lov uchun yangi chek yuboring."
            )
            return

        try:
            muammolar, takror, karta = await asyncio.to_thread(tekshir, data, user.id)
        except Exception:
            log.exception("Tekshiruvda xato")
            muammolar, takror, karta = [], None, None

        izoh = "; ".join(muammolar)
        if data.get("izoh"):
            izoh = f"{izoh}; {data['izoh']}" if izoh else data["izoh"]

        props = {
            "Ism": {"title": [{"text": {"content": student["ism"][:200]}}]},
            "O'quvchi": {"relation": [{"id": student["page_id"]}]},
            "Telegram ID": rt(user.id),
            "Summa": {"number": data.get("summa")},
            "Izoh": rt(izoh),
            "Tranzaksiya ID": rt(data.get("tranzaksiya_id")),
            "Fayl izi": rt(file_hash),
            "Qabul kartasi": rt(data.get("qabul_kartasi")),
        }
        if karta:
            props["Karta"] = {"relation": [{"id": karta["page_id"]}]}
        if data.get("sana"):
            props["Sana"] = {"date": {"start": data["sana"]}}

        upload_id = await asyncio.to_thread(
            upload_file, file_bytes, filename, media_type
        )
        if upload_id:
            props["Chek"] = {
                "files": [
                    {
                        "type": "file_upload",
                        "file_upload": {"id": upload_id},
                        "name": filename,
                    }
                ]
            }

        try:
            await asyncio.to_thread(ncreate, DB_PAYMENTS, props)
        except Exception:
            log.exception("Notion yozishda xato")
            await kutish.edit_text(
                "⚠️ Xatolik yuz berdi.\n\nIltimos, birozdan keyin qaytadan yuboring."
            )
            return

        # takroriy bo'lsa — eski yozuvga ham eslatma
        if takror:
            try:
                await asyncio.to_thread(
                    izoh_qoshish,
                    takror["id"],
                    txt(takror["properties"].get("Izoh", {})),
                    "shu kuni shu summa ikkinchi marta kelgan",
                )
            except Exception:
                log.exception("Eski yozuvga izoh yozilmadi")

    # --- karta xabarlarini o'chirish ---
    await kartani_tozalash(context, chat_id, context.user_data)

    await kutish.edit_text(
        f"✅ To'lovingiz qabul qilindi!\n\n"
        f"💰 {money(data.get('summa'))} so'm\n"
        f"📅 {sana_matn(data.get('sana'))}\n\n"
        f"Rahmat! 🌟"
    )

    # --- adminga xabar ---
    if muammolar:
        try:
            nik = f" (@{user.username})" if user.username else ""
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ Tekshirish kerak\n\n"
                f"👤 O'quvchi: {student['ism']}{nik}\n"
                f"💰 Summa: {money(data.get('summa'))} so'm\n"
                f"📅 Sana: {sana_matn(data.get('sana'))}\n"
                f"🏦 Bank: {data.get('bank') or '—'}\n\n"
                f"❗️ Sabab: {', '.join(muammolar)}",
            )
            if message.photo:
                await context.bot.send_photo(ADMIN_CHAT_ID, message.photo[-1].file_id)
            elif message.document:
                await context.bot.send_document(ADMIN_CHAT_ID, message.document.file_id)
        except Exception:
            log.exception("Adminga xabar yuborilmadi")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Chat ID: {chat.id}\nSizning ID: {update.effective_user.id}"
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("XATO: %s", context.error, exc_info=context.error)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_receipt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("To'lov boti ishga tushdi")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
