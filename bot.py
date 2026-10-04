"""
Academy to'lov boti — talaba chek yuboradi, AI o'qiydi, Notion'ga yoziladi.

Oqim:
  /start -> ism so'raladi (bir marta) -> "To'lov qilish" tugmasi
  -> faol karta ko'rsatiladi -> talaba chek yuboradi
  -> AI o'qiydi -> dublikat tekshiriladi -> Notion'ga yoziladi
  -> talabaga tasdiq, shubhali bo'lsa adminga xabar
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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
MAX_CHEK_YOSHI = 30  # kun — bundan eski chek shubhali

BTN_TOLOV = "💳 To'lov qilish"
BTN_TOLOVLARIM = "📋 Mening to'lovlarim"

MENU = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_TOLOV)], [KeyboardButton(BTN_TOLOVLARIM)]],
    resize_keyboard=True,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("tolov-bot")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

student_cache: dict[int, dict] = {}   # tg_id -> {"page_id":..., "ism":...}
card_cache: dict = {"vaqt": 0, "faol": None, "eski": []}
write_lock = asyncio.Lock()


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
    block = {
        "type": block_type,
        "source": {"type": "base64", "media_type": media_type, "data": b64},
    }

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
                    "content": [block, {"type": "text", "text": "Read this receipt."}],
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
    row = rows[0]
    info = {"page_id": row["id"], "ism": title_txt(row["properties"].get("Ism", {}))}
    student_cache[tg_id] = info
    return info


def create_student(tg_id: int, ism: str, username: str, profil: str) -> dict:
    def rt(v):
        return {"rich_text": [{"text": {"content": str(v)[:200]}}] if v else []}

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
    """Faol va eski kartalarni Notion'dan oladi, 5 daqiqa keshlanadi."""
    if time.time() - card_cache["vaqt"] < 300:
        return card_cache

    rows = nq(DB_CARDS, limit=50)
    faol, eski = None, []
    for row in rows:
        p = row["properties"]
        karta = {
            "raqam": title_txt(p.get("Karta raqami", {})),
            "egasi": txt(p.get("Egasi", {})),
            "bank": txt(p.get("Bank", {})),
        }
        holat = (p.get("Holati", {}).get("select") or {}).get("name")
        if holat == "Faol" and not faol:
            faol = karta
        elif holat == "Eski":
            eski.append(karta)

    card_cache.update({"vaqt": time.time(), "faol": faol, "eski": eski})
    return card_cache


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


# ----------------------------------------------------------------------------
# Tekshiruvlar
# ----------------------------------------------------------------------------

def tekshir(data: dict) -> list[str]:
    """Shubhali joylar ro'yxati. Bo'sh bo'lsa — hammasi joyida."""
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

    qabul = oxirgi4(data.get("qabul_kartasi"))
    if qabul:
        faol = cards.get("faol")
        if faol and oxirgi4(faol["raqam"]) == qabul:
            pass  # to'g'ri kartaga tushgan
        elif any(oxirgi4(k["raqam"]) == qabul for k in cards.get("eski", [])):
            muammolar.append("eski kartaga to'langan")
        else:
            muammolar.append(f"boshqa kartaga tushgan (...{qabul})")

    return muammolar


# ----------------------------------------------------------------------------
# Handlerlar
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    student = await asyncio.to_thread(get_student, user.id)

    if student:
        await update.message.reply_text(
            f"Assalomu alaykum, {student['ism']}!\n\n"
            f"To'lov qilish uchun pastdagi tugmani bosing.",
            reply_markup=MENU,
        )
        return

    context.user_data["kutilmoqda"] = "ism"
    await update.message.reply_text(
        "Assalomu alaykum!\n\n"
        "Ro'yxatdan o'tish uchun ism va familiyangizni yozing.\n"
        "Masalan: Aziza Karimova"
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
        if len(matn) < 3 or len(matn) > 60 or not re.search(r"[A-Za-zА-Яа-яЎўҚқҒғҲҳ]{2}", matn):
            await update.message.reply_text(
                "Iltimos, ism va familiyangizni to'liq yozing.\n"
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
        except Exception as exc:
            log.exception("Ro'yxatga yozishda xato")
            await update.message.reply_text("Xatolik yuz berdi, birozdan keyin urinib ko'ring.")
            return

        context.user_data.pop("kutilmoqda", None)
        await update.message.reply_text(
            f"Rahmat, {student['ism']}! Ro'yxatdan o'tdingiz.\n\n"
            f"To'lov qilish uchun pastdagi tugmani bosing.",
            reply_markup=MENU,
        )
        return

    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text(
            "Avval ism va familiyangizni yozing.\nMasalan: Aziza Karimova"
        )
        return

    await update.message.reply_text(
        "To'lov qilish uchun pastdagi tugmani bosing.", reply_markup=MENU
    )


async def show_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    student = await asyncio.to_thread(get_student, update.effective_user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text(
            "Avval ism va familiyangizni yozing.\nMasalan: Aziza Karimova"
        )
        return

    cards = await asyncio.to_thread(get_cards)
    faol = cards.get("faol")

    if not faol:
        await update.message.reply_text(
            "Hozircha to'lov kartasi mavjud emas. Iltimos, administratorga murojaat qiling."
        )
        log.error("Kartalar bazasida 'Faol' karta yo'q!")
        return

    satrlar = ["To'lovni quyidagi kartaga amalga oshiring:", ""]
    if faol["egasi"]:
        satrlar.append(f"Karta egasi: {faol['egasi']}")
    if faol["bank"]:
        satrlar.append(f"Bank: {faol['bank']}")
    satrlar += ["", "Karta raqami pastda — bosib nusxalashingiz mumkin."]

    await update.message.reply_text("\n".join(satrlar))
    await update.message.reply_text(f"`{faol['raqam']}`", parse_mode="MarkdownV2")
    await update.message.reply_text(
        "To'lovni amalga oshirgach, chek rasmini (screenshot) shu yerga yuboring."
    )


async def show_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    student = await asyncio.to_thread(get_student, update.effective_user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await update.message.reply_text("Avval ism va familiyangizni yozing.")
        return

    rows = await asyncio.to_thread(
        nq,
        DB_PAYMENTS,
        {"property": "Telegram ID", "rich_text": {"equals": str(update.effective_user.id)}},
        [{"property": "Sana", "direction": "ascending"}],
        100,
    )

    if not rows:
        await update.message.reply_text("Hozircha to'lovlaringiz yo'q.", reply_markup=MENU)
        return

    satrlar, jami = [], 0.0
    for i, row in enumerate(rows, 1):
        p = row["properties"]
        summa = p.get("Summa", {}).get("number")
        sana = (p.get("Sana", {}).get("date") or {}).get("start")
        jami += summa or 0
        satrlar.append(f"{i}. {money(summa)} so'm — {sana_matn(sana)}")

    await update.message.reply_text(
        "To'lovlaringiz:\n\n" + "\n".join(satrlar) + f"\n\nJami: {money(jami)} so'm",
        reply_markup=MENU,
    )


async def on_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message

    student = await asyncio.to_thread(get_student, user.id)
    if not student:
        context.user_data["kutilmoqda"] = "ism"
        await message.reply_text(
            "Avval ism va familiyangizni yozing.\nMasalan: Aziza Karimova"
        )
        return

    # --- faylni olish ---
    if message.photo:
        tg_file = await context.bot.get_file(message.photo[-1].file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        media_type, filename = "image/jpeg", f"chek_{message.message_id}.jpg"
    else:
        doc = message.document
        mt = (doc.mime_type or "").lower()
        if mt not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
            await message.reply_text("Faqat rasm yoki PDF yuboring.")
            return
        if doc.file_size and doc.file_size > 18 * 1024 * 1024:
            await message.reply_text("Fayl juda katta. Kichikroq rasm yuboring.")
            return
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        media_type = mt
        filename = doc.file_name or f"chek_{message.message_id}"

    kutish = await message.reply_text("Chek tekshirilmoqda...")

    try:
        data = await asyncio.to_thread(call_claude, file_bytes, media_type)
    except Exception as exc:
        log.exception("AI xatosi")
        await kutish.edit_text("Chekni o'qib bo'lmadi. Iltimos, qaytadan yuboring.")
        return

    log.info("Transkript (%s): %s", student["ism"], data.get("_transkript", "")[:400])

    if data.get("chek_emas"):
        await kutish.edit_text(
            "Bu to'lov chekiga o'xshamadi. Iltimos, chek screenshotini yuboring."
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
                "Bu chek avval yuborilgan. Yangi to'lov uchun yangi chek yuboring."
            )
            return

        muammolar = await asyncio.to_thread(tekshir, data)

        izoh = "; ".join(muammolar)
        if data.get("izoh"):
            izoh = f"{izoh}; {data['izoh']}" if izoh else data["izoh"]

        def rt(v):
            return {"rich_text": [{"text": {"content": str(v)[:1900]}}] if v else []}

        props = {
            "Ism": {"title": [{"text": {"content": student["ism"][:200]}}]},
            "O'quvchi": {"relation": [{"id": student["page_id"]}]},
            "Telegram ID": rt(user.id),
            "Summa": {"number": data.get("summa")},
            "Izoh": rt(izoh),
            "Tranzaksiya ID": rt(data.get("tranzaksiya_id")),
            "Fayl izi": rt(file_hash),
        }

        if data.get("sana"):
            props["Sana"] = {"date": {"start": data["sana"]}}

        upload_id = await asyncio.to_thread(upload_file, file_bytes, filename, media_type)
        if upload_id:
            props["Chek"] = {
                "files": [
                    {"type": "file_upload", "file_upload": {"id": upload_id}, "name": filename}
                ]
            }

        try:
            await asyncio.to_thread(ncreate, DB_PAYMENTS, props)
        except Exception as exc:
            log.exception("Notion yozishda xato")
            await kutish.edit_text(
                "Xatolik yuz berdi. Iltimos, birozdan keyin qaytadan yuboring."
            )
            return

    # --- talabaga javob ---
    await kutish.edit_text(
        f"✅ To'lovingiz qabul qilindi\n"
        f"{money(data.get('summa'))} so'm — {sana_matn(data.get('sana'))}"
    )

    # --- adminga xabar ---
    if muammolar:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ Tekshirish kerak\n\n"
                f"O'quvchi: {student['ism']}"
                + (f" (@{user.username})" if user.username else "")
                + f"\nSumma: {money(data.get('summa'))} so'm\n"
                f"Sana: {sana_matn(data.get('sana'))}\n"
                f"Bank: {data.get('bank') or '—'}\n"
                f"Sabab: {', '.join(muammolar)}",
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
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, on_receipt)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("To'lov boti ishga tushdi")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
