import logging
import os
import re
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

import asyncpg

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -------------------- Config --------------------
ADMIN_ID = 165553982

# Uzbek Latin apostrophe output (as in Oʻ, Gʻ)
APOS_OUT = "ʻ"  # U+02BB

# Separator apostrophe output for Cyrillic hard sign Ъ/ъ (ayirish belgisi)
SEP_APOS_OUT = "’"  # U+2019

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiril_lotin_auto")


# -------------------- Supabase PostgreSQL persistence --------------------
# This bot uses its own table, leaving other Supabase bot tables untouched.
USERS: List[int] = []
USER_LANG: Dict[int, str] = {}


async def _db_connect() -> asyncpg.Connection:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL topilmadi. Render Environment'ga Supabase PostgreSQL ulanish URL'ini kiriting.")
    # Supabase transaction pooler: disable asyncpg's named prepared-statement cache.
    return await asyncpg.connect(dsn=url, ssl="require", statement_cache_size=0, timeout=20)


async def _initialize_db() -> None:
    conn = await _db_connect()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kiril_lotin_users (
                chat_id BIGINT PRIMARY KEY,
                lang TEXT CHECK (lang IN ('uz', 'ru') OR lang IS NULL),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    finally:
        await conn.close()


async def _load_state() -> None:
    global USERS, USER_LANG
    conn = await _db_connect()
    try:
        rows = await conn.fetch("SELECT chat_id, lang FROM kiril_lotin_users ORDER BY created_at, chat_id")
        USERS = [int(row["chat_id"]) for row in rows]
        USER_LANG = {int(row["chat_id"]): row["lang"] for row in rows if row["lang"] in ("uz", "ru")}
    finally:
        await conn.close()


async def _save_user(chat_id: int, lang: str | None = None) -> None:
    conn = await _db_connect()
    try:
        await conn.execute("""
            INSERT INTO kiril_lotin_users (chat_id, lang) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET
                lang = COALESCE(EXCLUDED.lang, kiril_lotin_users.lang)
        """, chat_id, lang)
    finally:
        await conn.close()


async def _remove_users(ids: List[int]) -> None:
    if not ids:
        return
    conn = await _db_connect()
    try:
        await conn.execute("DELETE FROM kiril_lotin_users WHERE chat_id = ANY($1::bigint[])", ids)
    finally:
        await conn.close()


async def _post_init(app) -> None:
    # Fail startup if Supabase is unavailable rather than silently losing user data.
    await _initialize_db()
    await _load_state()
    logger.info("Supabase PostgreSQL tayyor. Users: %d", len(USERS))


# -------------------- Normalization --------------------

APOSTROPHES = {
    "'", "`", "´",
    "\u2019",  # ’
    "\u2018",  # ‘
    "\u02BC",  # ʼ
    "\u02BB",  # ʻ / ʻ-like
    "\u201B",  # ‛
    "\u2032",  # ′
}


def normalize_apostrophes(text: str) -> str:
    # Normalize any apostrophe-like symbol to ASCII apostrophe for parsing.
    for a in APOSTROPHES:
        text = text.replace(a, "'")
    return text


# -------------------- Transliteration maps (docx-based) --------------------
# Uzbek mapping per user's doc:
# Q<->Қ, Oʻ<->Ў, Gʻ<->Ғ, H<->Ҳ, Sh<->Ш, Ch<->Ч, ng<->нг, E<->Э, and ’<->ъ.

# -------------------- Word-level exceptions (imlo tavsiyalari) --------------------
# IMPORTANT:
# The large "imlo lug'ati" PDF that you shared appears to be mostly scanned images
# (text is not extractable programmatically here), so we cannot automatically build
# a complete dictionary of all words.
#
# What we CAN do safely:
# 1) Implement rule-based conversion (already present).
# 2) Add a small, curated exceptions list from the 2013 recommendations/examples.
#
# If you later provide a machine-readable list (TXT/CSV/Excel) of "latin <-> kirill"
# pairs, we can bulk-import it into this exceptions section.

# Cyrillic -> Latin word exceptions (lowercase keys). Output is Uzbek Latin.
CYR_LAT_WORD_EXCEPTIONS: Dict[str, str] = {
    # Ё/Ю/Я examples where spelling is fixed as per recommendations/examples
    "стажёр": "stajor",
    "люстра": "lustra",
    "фляга": "flaga",
    "актёр": "aktyor",
    "бюро": "byuro",
    "снаряд": "snaryad",

    # Ц examples (Cyrillic uses Ц, Latin is often written with S or TS)
    "цех": "sex",
    "цирк": "sirk",
    "кварц": "kvars",
    "шприц": "shpris",
    "целлофан": "sellofan",
    "цилиндр": "silindr",
    "антициклон": "antisiklon",
    "конструкция": "konstruksiya",
    "акция": "aksiya",
    "лицей": "litsey",
    "офицер": "ofitser",
    "доцент": "dotsent",
    "конституция": "konstitutsiya",
}

# Latin -> Cyrillic reverse exceptions (lowercase keys). Output is Uzbek Cyrillic.
LAT_CYR_WORD_EXCEPTIONS: Dict[str, str] = {v: k for k, v in CYR_LAT_WORD_EXCEPTIONS.items()}


def _match_token_case(value: str, template: str) -> str:
    """Apply casing style of `template` word to `value`.

    - ALLCAPS template -> ALLCAPS value
    - TitleCase template -> TitleCase value (first letter upper, rest lower)
    - otherwise -> lowercase value
    """
    if not value:
        return value
    # Python's isupper/istitle ignore punctuation/apostrophes which is what we want
    if template.isupper():
        return value.upper()
    if template[:1].isupper() and (template[1:].islower() or len(template) == 1):
        return value[:1].upper() + value[1:]
    return value.lower()


# Tokenization for word-level processing (letters in Latin/Cyrillic + apostrophe)
WORD_RE = re.compile(r"[A-Za-zĞğÖöŞşÇç\u0400-\u04FF']+")

LAT_SINGLE_TO_CYR: Dict[str, str] = {
    "a": "а",
    "b": "б",
    "d": "д",
    "e": "э",  # E -> Э
    "f": "ф",
    "g": "г",
    "h": "ҳ",
    "i": "и",
    "j": "ж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "қ",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "x": "х",
    "y": "й",
    "z": "з",
    # Optional compatibility letters:
    "c": "ц",
    "ğ": "ғ",
    "ö": "ў",
    "ş": "ш",
    "ç": "ч",
}

CYR_SINGLE_TO_LAT: Dict[str, str] = {
    "а": "a",
    "б": "b",
    "д": "d",
    "э": "e",
    "ф": "f",
    "г": "g",
    "ҳ": "h",
    "и": "i",
    "ж": "j",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "қ": "q",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "в": "v",
    "х": "x",
    "й": "y",
    "з": "z",
    # "ц" is context-dependent (see cyr_to_lat rules), keep a safe default here.
    "ц": "s",

    "ў": "ö",
    "ғ": "ğ",
    "ш": "ş",
    "ч": "ç",

    # IMPORTANT: hard sign Ъ/ъ -> separator apostrophe ’ (Ya’ni, E’LON)
    "ъ": SEP_APOS_OUT,

    # Optional: soft sign
    "ь": "'",

    # Common extra letters in Uzbek Cyrillic texts
    "ё": "yo",
    "ю": "yu",
    "я": "ya",
    "е": "e",  # context will override
}

CYR_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ") | {"ў", "Ў"}


def _is_cyrillic(text: str) -> bool:
    # Detect any Cyrillic letters (including Uzbek-specific)
    return re.search(r"[\u0400-\u04FF]", text) is not None


def _new_latin(text: str) -> str:
    """Use the new four Latin letters in existing exception spellings."""
    return (text.replace("sh", "ş").replace("ch", "ç")
            .replace("g'", "ğ").replace("o'", "ö"))


LAT_CYR_WORD_EXCEPTIONS.update({
    _new_latin(lat): cyr for lat, cyr in list(LAT_CYR_WORD_EXCEPTIONS.items())
})


def cyr_to_lat(text: str) -> str:
    """Cyrillic -> Latin (word-aware), with rules for Е/е and Ц/ц plus exceptions."""
    text = normalize_apostrophes(text)

    def _convert_token(token: str) -> str:
        # If token has no Cyrillic letters, return as-is.
        if not _is_cyrillic(token):
            return token

        low_tok = token.casefold()
        if low_tok in CYR_LAT_WORD_EXCEPTIONS:
            return _match_token_case(_new_latin(CYR_LAT_WORD_EXCEPTIONS[low_tok]), token)

        out: List[str] = []

        def _next_alpha_is_upper(pos: int) -> bool:
            k = pos + 1
            while k < len(token):
                c = token[k]
                if c.isalpha():
                    return c.isupper()
                k += 1
            return False

        for i, ch in enumerate(token):
            # Context rule for Ц/ц (Ts digraph): after vowel -> ts, else -> s
            if ch in ("ц", "Ц"):
                prev = token[i - 1] if i > 0 else ""
                at_word_start = (i == 0) or (not prev.isalpha())
                after_vowel = (prev in CYR_VOWELS)
                use_ts = (not at_word_start) and after_vowel

                if ch == "Ц":
                    if use_ts:
                        out.append("TS" if token.isupper() or _next_alpha_is_upper(i) else "Ts")
                    else:
                        out.append("S")
                else:
                    out.append("ts" if use_ts else "s")
                continue

            # Context rule for Е/е -> ye/e
            if ch in ("е", "Е"):
                prev = token[i - 1] if i > 0 else ""
                at_word_start = (i == 0) or (not prev.isalpha())
                after_vowel_or_sign = (prev in CYR_VOWELS) or (prev in ("ъ", "ь", "Ъ", "Ь"))
                use_ye = at_word_start or after_vowel_or_sign
                if ch == "Е":
                    if use_ye:
                        out.append("YE" if token.isupper() or _next_alpha_is_upper(i) else "Ye")
                    else:
                        out.append("E")
                else:
                    out.append("ye" if use_ye else "e")
                continue

            low = ch.lower()
            if low in CYR_SINGLE_TO_LAT:
                lat = CYR_SINGLE_TO_LAT[low]
                if ch.isupper():
                    if len(lat) == 1:
                        out.append(lat.upper())
                    else:
                        out.append(lat[0].upper() + lat[1:])
                else:
                    out.append(lat)
            else:
                out.append(ch)

        return "".join(out)

    return WORD_RE.sub(lambda m: _convert_token(m.group(0)), text)


def _apply_case_single(cyr: str, token: str) -> str:
    """Case for single-letter Cyrillic outputs"""
    if token.isupper():
        return cyr.upper()
    if token[:1].isupper():
        return cyr.upper()
    return cyr


def _case_ng(token: str) -> str:
    # 'ng' -> 'нг'
    if token.isupper():
        return "НГ"
    if token[:1].isupper():
        return "Нг"
    return "нг"


def lat_to_cyr(text: str) -> str:
    """Latin -> Cyrillic (word-aware), based on user's docx mapping + exceptions."""
    text = normalize_apostrophes(text)

    LAT_VOWELS = set("aeiouöAEIOUÖ")

    def _convert_token(token: str) -> str:
        # If token contains Cyrillic already, leave it.
        if _is_cyrillic(token):
            return token

        low_tok = token.casefold()
        if low_tok in LAT_CYR_WORD_EXCEPTIONS:
            return _match_token_case(LAT_CYR_WORD_EXCEPTIONS[low_tok], token)

        i = 0
        out: List[str] = []

        while i < len(token):
            # Special case: "yo'" / "yoʻ" should become "йў" (not "ёъ")
            # We first emit Й/й for the leading 'y', then let the next loop iteration
            # convert "o'" -> Ў/ў via the existing rule.
            if i + 2 < len(token) and token[i].lower() == "y" and token[i + 1].lower() == "o" and token[i + 2] == "'":
                out.append("Й" if token[i].isupper() else "й")
                i += 1
                continue

            chunk2 = token[i:i + 2]

            # Ts digraph -> Ц/ц
            if len(chunk2) >= 2 and chunk2.lower() == "ts":
                out.append("Ц" if chunk2[:1].isupper() else "ц")
                i += 2
                continue

            # O' -> Ў/ў
            if len(chunk2) >= 2 and chunk2[0].lower() == "o" and chunk2[1] == "'":
                out.append("Ў" if chunk2[0].isupper() else "ў")
                i += 2
                continue

            # G' -> Ғ/ғ
            if len(chunk2) >= 2 and chunk2[0].lower() == "g" and chunk2[1] == "'":
                out.append("Ғ" if chunk2[0].isupper() else "ғ")
                i += 2
                continue

            # Sh / Ch
            if len(chunk2) >= 2 and chunk2.lower() == "sh":
                out.append("Ш" if chunk2[:1].isupper() else "ш")
                i += 2
                continue
            if len(chunk2) >= 2 and chunk2.lower() == "ch":
                out.append("Ч" if chunk2[:1].isupper() else "ч")
                i += 2
                continue

            # Ng -> нг
            if len(chunk2) >= 2 and chunk2.lower() == "ng":
                out.append(_case_ng(chunk2))
                i += 2
                continue

            # Yo/Yu/Ya/Ye digraphs
            if len(chunk2) >= 2 and chunk2.lower() == "yo":
                out.append("Ё" if chunk2[:1].isupper() else "ё")
                i += 2
                continue
            if len(chunk2) >= 2 and chunk2.lower() == "yu":
                out.append("Ю" if chunk2[:1].isupper() else "ю")
                i += 2
                continue
            if len(chunk2) >= 2 and chunk2.lower() == "ya":
                out.append("Я" if chunk2[:1].isupper() else "я")
                i += 2
                continue
            if len(chunk2) >= 2 and chunk2.lower() == "ye":
                out.append("Е" if chunk2[:1].isupper() else "е")
                i += 2
                continue

            ch = token[i]

            # Contextual E/e:
            #   - word start OR after vowel -> Э/э
            #   - after consonant           -> Е/е
            if ch.lower() == "e":
                prev = token[i - 1] if i > 0 else ""
                at_word_start = (i == 0) or (not prev.isalpha())
                after_vowel = (not at_word_start) and (prev in LAT_VOWELS)
                use_eh = at_word_start or after_vowel

                if use_eh:
                    out.append("Э" if ch.isupper() else "э")
                else:
                    out.append("Е" if ch.isupper() else "е")
                i += 1
                continue

            # Standalone apostrophe -> Ъ/ъ (case depends on next Latin letter)
            if ch == "'":
                k = i + 1
                next_alpha: str = ""
                while k < len(token):
                    if token[k].isalpha():
                        next_alpha = token[k]
                        break
                    k += 1
                use_upper = bool(next_alpha and next_alpha.isupper())
                out.append("Ъ" if use_upper else "ъ")
                i += 1
                continue

            low = ch.lower()
            if low in LAT_SINGLE_TO_CYR:
                cyr = LAT_SINGLE_TO_CYR[low]
                out.append(_apply_case_single(cyr, ch))
            else:
                out.append(ch)
            i += 1

        return "".join(out)

    return WORD_RE.sub(lambda m: _convert_token(m.group(0)), text)


def auto_convert(text: str) -> str:
    if _is_cyrillic(text):
        return cyr_to_lat(text)
    return lat_to_cyr(text)


# -------------------- UI texts --------------------

def _greeting_uz() -> str:
    return (
        "👋🏻 <b>Salom</b>\n"
        "Ushbu bot <b>Lotin</b>chani -> <b>Krill</b>chaga yoki <b>Krill</b>chani -> <b>Lotin</b>chaga be’xato o‘zgartirib beradi.\n\n"
        "✅ <b>@Kril_Lotin_uzb_bot ning imkoniyatlari:</b>\n"
        "🔁 O‘zbekcha <b>Krill</b> -> <b>Lotin</b> yoki O‘zbekcha <b>Lotin</b> -> <b>Krill</b> harflariga aylantirish!\n"
        "✅ O‘girishda Krillcha <b>Ў</b>, <b>Қ</b>, <b>Ғ</b>, <b>Ҳ</b>, <b>Ъ</b> harflarini qoida bo‘yicha <b>bexato bajaradi</b>.\n\n"
        "Bizning foydali botlar kanaliga a'zo bo'ling:\n"
        "<b>https://t.me/+skp5TgimYIJjYzIy</b>\n\n"
        "🔗 <b>BOSHLASH UCHUN MENGA LOTINCHA YOKI KIRILLCHA YOZING</b> ⤵️"
    )



def _greeting_ru() -> str:
    return (
        "👋🏻 <b>Привет</b>.\n"
        "Этот бот без ошибок преобразует <b>латиницу</b> -> <b>кириллицу</b> или <b>кириллицу</b> -> <b>латиницу</b>.\n\n"
        "✅ <b>Возможности @Kril_Lotin_uzb_bot:</b>\n"
        "🔁 Узбекский <b>кирилл</b> -> <b>латин</b> или Узбекский <b>латин</b> -> <b>кирилл</b>!\n"
        "✅ По правилам корректно обрабатывает буквы <b>Ў</b>, <b>Қ</b>, <b>Ғ</b>, <b>Ҳ</b>, <b>Ъ</b>.\n\n"
        "Подпишитесь на наш канал полезных ботов:\n"
        "<b>https://t.me/+skp5TgimYIJjYzIy</b>\n\n"
        "🔗 <b>ДЛЯ НАЧАЛА НАПИШИТЕ МНЕ ТЕКСТ ЛАТИНИЦЕЙ ИЛИ КИРИЛЛИЦЕЙ</b> ⤵️"
    )



def _language_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("O'zbekcha", callback_data="lang:uz"),
            InlineKeyboardButton("Русский", callback_data="lang:ru"),
        ]
    ]
    return InlineKeyboardMarkup(kb)


# -------------------- Handlers --------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show language selection on /start and save user for broadcasts."""
    global USERS

    chat = update.effective_chat
    user = update.effective_user

    # Save private chat users for broadcast
    if chat and chat.type == "private":
        cid = chat.id
        if cid not in USERS:
            await _save_user(cid)
            USERS.append(cid)

    await update.effective_message.reply_text(
        "Tilni tanlang / Выбрать язык",
        reply_markup=_language_keyboard(),
    )


async def lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set language and show greeting."""
    global USER_LANG

    query = update.callback_query
    if not query or not query.data:
        return

    m = re.match(r"^lang:(uz|ru)$", query.data)
    if not m:
        return

    lang = m.group(1)
    uid = query.from_user.id
    await _save_user(uid, lang)
    if uid not in USERS:
        USERS.append(uid)
    USER_LANG[uid] = lang

    # UI
    try:
        await query.answer("✅")
    except Exception:
        pass

    greeting = _greeting_uz() if lang == "uz" else _greeting_ru()

    # Replace the selection message with greeting
    try:
        await query.message.edit_text(greeting, disable_web_page_preview=True, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=greeting,
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


def _is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    text = " ".join(context.args or []).strip()
    if not text:
        return await update.effective_message.reply_text("Foydalanish: /broadcast <matn>")

    ok = 0
    bad = 0
    removed: List[int] = []

    # Refresh from Supabase before sending, including after Render restarts.
    await _load_state()
    for cid in list(USERS):
        try:
            await context.bot.send_message(chat_id=cid, text=text)
            ok += 1
        except Forbidden:
            bad += 1
            removed.append(cid)
        except BadRequest:
            bad += 1
            removed.append(cid)
        except Exception:
            bad += 1
        await asyncio.sleep(0.03)

    if removed:
        await _remove_users(removed)
        USERS[:] = [x for x in USERS if x not in set(removed)]
        for cid in removed:
            USER_LANG.pop(cid, None)

    await update.effective_message.reply_text(f"✅ Yuborildi: {ok} ta\n❌ Yetmadi: {bad} ta")


async def broadcastpost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    msg = update.effective_message
    if not msg.reply_to_message:
        return await msg.reply_text("Foydalanish: biror xabarga reply qilib /broadcastpost yozing.")

    src = msg.reply_to_message

    ok = 0
    bad = 0
    removed: List[int] = []

    # Refresh from Supabase before sending, including after Render restarts.
    await _load_state()
    for cid in list(USERS):
        try:
            await context.bot.copy_message(
                chat_id=cid,
                from_chat_id=src.chat.id,
                message_id=src.message_id,
            )
            ok += 1
        except Forbidden:
            bad += 1
            removed.append(cid)
        except BadRequest:
            bad += 1
            removed.append(cid)
        except Exception:
            bad += 1
        await asyncio.sleep(0.03)

    if removed:
        await _remove_users(removed)
        USERS[:] = [x for x in USERS if x not in set(removed)]
        for cid in removed:
            USER_LANG.pop(cid, None)

    await msg.reply_text(f"✅ Yuborildi: {ok} ta\n❌ Yetmadi: {bad} ta")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text:
        return

    # Ignore commands
    if text.startswith("/"):
        return

    converted = auto_convert(text)
    await msg.reply_text(converted)


# -------------------- Render HTTP health endpoint --------------------
# Render Web Service requires an HTTP listener. Telegram uses polling as before.
def _start_render_http_server() -> None:
    if not (os.getenv("RENDER") or os.getenv("PORT")):
        return  # Local Windows use needs no HTTP server.

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                body = b"Telegram bot process running"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, format, *args):
            pass

    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Render HTTP health endpoint: 0.0.0.0:%s", port)


def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN topilmadi. Render Environment yoki lokal .env faylida BOT_TOKEN ni sozlang.")

    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL topilmadi. Supabase PostgreSQL URL'ini sozlang.")

    _start_render_http_server()

    app = ApplicationBuilder().token(token).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(lang_callback, pattern=r"^lang:(uz|ru)$"))

    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("broadcastpost", broadcastpost_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot ishga tushdi. Users: %d", len(USERS))

    # Python 3.14 no longer creates a main-thread event loop implicitly.
    # Create and register one before PTB starts polling. This also keeps the
    # startup path compatible with older python-telegram-bot releases.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
