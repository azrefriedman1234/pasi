import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_from_directory,
    flash,
    session,
)

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)

from PIL import Image, ImageFilter

# --------------------------------------------------------------------------
# הגדרות כלליות
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
SETTINGS_PATH = BASE_DIR / "settings.json"
SESSION_PATH = DATA_DIR / "telegram.session"
WATERMARK_PATH = DATA_DIR / "watermark.png"

DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("PASIFLONET_SECRET_KEY", "dev-secret-key")


# --------------------------------------------------------------------------
# עבודה עם settings.json
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "telegram_api_id": "",
    "telegram_api_hash": "",
    "telegram_phone": "",
    "telegram_password": "",
    "telegram_target": "",  # ערוץ / יוזר יעד לשליחה
    # לשימוש פנימי בזרימת קוד:
    "telegram_phone_code_hash": "",
    "telegram_phone_for_login": "",
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        logging.warning("settings.json לא קיים – נוצר קובץ ברירת מחדל")
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logging.exception("שגיאה בקריאת settings.json – נטען ברירת מחדל")
        data = {}
    merged = DEFAULT_SETTINGS.copy()
    merged.update(data)
    return merged


def save_settings(settings: dict) -> None:
    try:
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("שגיאה בשמירת settings.json")


# --------------------------------------------------------------------------
# עזר להתחברות לאתר (סיסמת 7447)
# --------------------------------------------------------------------------

LOGIN_PASSWORD = "7447"


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# עזר טלגרם – בניית Client
# --------------------------------------------------------------------------

def _build_telegram_client(api_id: int, api_hash: str) -> TelegramClient:
    """
    יוצר TelegramClient עם קובץ סשן קבוע (SESSION_PATH).
    """
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    return client


async def _send_telegram_code_async(api_id: int, api_hash: str, phone: str) -> str:
    """
    שולח קוד התחברות לטלגרם ומחזיר phone_code_hash.
    """
    client = _build_telegram_client(api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(phone)
        logging.info("Telegram code sent, phone_code_hash received")
        return result.phone_code_hash
    finally:
        await client.disconnect()


async def _login_telegram_async(
    api_id: int,
    api_hash: str,
    phone: str,
    code: str,
    password: str | None,
    phone_code_hash: str,
) -> None:
    """
    לוגין לטלגרם בעזרת קוד שנשלח, כולל 2FA אם צריך.
    משתמש ב-phone_code_hash שנשמר קודם.
    """
    client = _build_telegram_client(api_id, api_hash)
    try:
        await client.connect()

        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except SessionPasswordNeededError:
            # יש אימות דו־שלבי
            await client.sign_in(password=password or "")

        if not await client.is_user_authorized():
            raise RuntimeError("המשתמש עדיין לא מאומת בטלגרם")

        logging.info("Telegram login success, session saved at %s", SESSION_PATH)

    except PhoneCodeInvalidError:
        logging.exception("קוד האימות שגוי")
        raise RuntimeError("קוד האימות שגוי – נסה שוב.")
    except PhoneCodeExpiredError:
        logging.exception("קוד האימות פג תוקף")
        raise RuntimeError("קוד האימות פג תוקף – בקש קוד חדש.")
    finally:
        await client.disconnect()


async def _send_telegram_message_async(
    api_id: int,
    api_hash: str,
    text: str,
    target: str,
    media_path: str | None = None,
) -> None:
    """
    שולח הודעה / מדיה לטלגרם ליעד מסוים (ערוץ / יוזר).
    מניח שכבר יש סשן מאומת (SESSION_PATH).
    """
    client = _build_telegram_client(api_id, api_hash)
    try:
        await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError("סשן טלגרם לא מאומת – התחבר מחדש דרך ההגדרות.")

        if media_path:
            await client.send_file(target, media_path, caption=text or None)
        else:
            await client.send_message(target, text)

        logging.info("Telegram message sent to %s", target)

    finally:
        await client.disconnect()


async def _fetch_messages_from_all_dialogs_async(
    api_id: int,
    api_hash: str,
    limit_per_dialog: int = 5,
    max_total: int = 120,
) -> list[dict]:
    """
    מושך הודעות אחרונות מכל הדיאלוגים (ערוצים, קבוצות, צ’אטים).
    מוחזר כ-list של dict, בלי לשמור לקובץ.
    """
    client = _build_telegram_client(api_id, api_hash)
    items: list[dict] = []
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logging.warning("Telegram session not authorized – no messages fetched")
            return []

        async for dialog in client.iter_dialogs():
            # אפשר לסנן כאן רק ערוצים/קבוצות/יוזרים – נשאיר הכל
            try:
                async for msg in client.iter_messages(dialog, limit=limit_per_dialog):
                    if not (msg.message or msg.media):
                        continue

                    ts = msg.date  # timezone-aware UTC
                    items.append(
                        {
                            "dialog_id": dialog.id,
                            "dialog_title": dialog.name or "",
                            "message_id": msg.id,
                            "text": msg.message or "",
                            "has_media": bool(msg.media),
                            "date": ts,
                            "date_str": ts.astimezone().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        }
                    )
            except Exception:
                logging.exception("Error fetching messages for dialog %s", dialog.id)
                continue

        # מיון לפי זמן (חדש קודם)
        items.sort(key=lambda x: x["date"], reverse=True)
        if len(items) > max_total:
            items = items[:max_total]

        return items

    finally:
        await client.disconnect()


# --------------------------------------------------------------------------
# פונקציות טשטוש / סימן מים לתמונה (בשלב ראשון – רק תמונות סטילס)
# --------------------------------------------------------------------------

def apply_blur_and_watermark(
    input_path: Path,
    blur: bool,
    use_watermark: bool,
) -> Path:
    """
    טשטוש + סימן מים לתמונה (לוידאו צריך FFMPEG – אפשר להרחיב אחר כך).
    מחזיר את הנתיב לקובץ המעובד (שומר כ- *_proc.png).
    """
    img = Image.open(input_path).convert("RGBA")

    if blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=20))

    if use_watermark and WATERMARK_PATH.exists():
        try:
            wm = Image.open(WATERMARK_PATH).convert("RGBA")
            # שינוי גודל סימן מים ל־20% מרוחב התמונה
            base_w, base_h = img.size
            ratio = 0.2
            new_w = int(base_w * ratio)
            wm_ratio = wm.height / wm.width
            new_h = int(new_w * wm_ratio)
            wm = wm.resize((new_w, new_h))

            # מיקום – פינה ימנית תחתונה
            pos = (base_w - new_w - 10, base_h - new_h - 10)

            img.alpha_composite(wm, dest=pos)
        except Exception:
            logging.exception("שגיאה בהוספת סימן מים")

    out_path = input_path.with_name(input_path.stem + "_proc.png")
    img.save(out_path, format="PNG")
    return out_path


# --------------------------------------------------------------------------
# ראוטים
# --------------------------------------------------------------------------

@app.route("/")
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return redirect(url_for("messages"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == LOGIN_PASSWORD:
            session["logged_in"] = True
            flash("ברוך הבא לפסיפלונט Web ✔", "success")
            return redirect(url_for("messages"))
        else:
            flash("סיסמה שגויה", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("התנתקת מהמערכת", "info")
    return redirect(url_for("login"))


@app.route("/messages")
@login_required
def messages():
    """
    מציג הודעות מכל הערוצים/צ’אטים – נשלפות כל פעם מחדש מטלגרם
    לפי הסשן שנשמר.
    """
    settings = load_settings()
    api_id = int(settings.get("telegram_api_id") or 0)
    api_hash = settings.get("telegram_api_hash") or ""

    telegram_connected = False
    messages_list: list[dict] = []

    if api_id and api_hash and SESSION_PATH.exists():
        try:
            messages_list = asyncio.run(
                _fetch_messages_from_all_dialogs_async(api_id, api_hash)
            )
            telegram_connected = True
        except Exception as e:
            logging.exception("Error fetching Telegram messages: %s", e)
            flash(
                "שגיאה במשיכת הודעות מטלגרם – בדוק את ההתחברות בהגדרות.",
                "danger",
            )
    else:
        flash("עדיין לא מוגדר חיבור לטלגרם (API / סשן).", "warning")

    return render_template(
        "messages.html",
        messages=messages_list,
        telegram_connected=telegram_connected,
    )


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_message():
    settings = load_settings()
    api_id = int(settings.get("telegram_api_id") or 0)
    api_hash = settings.get("telegram_api_hash") or ""
    target = settings.get("telegram_target") or ""

    if request.method == "POST":
        text = request.form.get("text", "").strip()
        apply_blur_flag = bool(request.form.get("apply_blur"))
        apply_wm_flag = bool(request.form.get("apply_watermark"))

        if not api_id or not api_hash:
            flash("חסרים API ID / API HASH בהגדרות טלגרם.", "danger")
            return redirect(url_for("new_message"))

        if not target:
            flash("לא הוגדר ערוץ / יעד טלגרם בהגדרות.", "danger")
            return redirect(url_for("new_message"))

        media_file = request.files.get("media")
        media_path = None

        if media_file and media_file.filename:
            filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{media_file.filename}"
            save_path = MEDIA_DIR / filename
            media_file.save(save_path)
            media_path = str(save_path)

            # אם זה תמונה – ניישם טשטוש / סימן מים
            if apply_blur_flag or apply_wm_flag:
                try:
                    processed = apply_blur_and_watermark(
                        save_path,
                        blur=apply_blur_flag,
                        use_watermark=apply_wm_flag,
                    )
                    media_path = str(processed)
                except Exception:
                    logging.exception("שגיאה בעיבוד תמונה")
                    flash("שגיאה בעיבוד התמונה (טשטוש / סימן מים).", "danger")

        try:
            asyncio.run(
                _send_telegram_message_async(
                    api_id=api_id,
                    api_hash=api_hash,
                    text=text,
                    target=target,
                    media_path=media_path,
                )
            )
            flash("ההודעה נשלחה לטלגרם ✔", "success")
            return redirect(url_for("messages"))
        except Exception as e:
            logging.exception("Telegram send failed: %s", e)
            flash(f"שגיאה בשליחת הודעה לטלגרם: {e}", "danger")
            return redirect(url_for("new_message"))

    return render_template("new_message.html")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    settings = load_settings()

    if request.method == "POST":
        action = request.form.get("action", "save")

        # עדכון ערכים בסיסיים
        settings["telegram_api_id"] = request.form.get("telegram_api_id", "").strip()
        settings["telegram_api_hash"] = request.form.get("telegram_api_hash", "").strip()
        settings["telegram_phone"] = request.form.get("telegram_phone", "").strip()
        settings["telegram_password"] = request.form.get("telegram_password", "").strip()
        settings["telegram_target"] = request.form.get("telegram_target", "").strip()

        # שמירת watermark אם הועלה
        wm_file = request.files.get("watermark_image")
        if wm_file and wm_file.filename:
            WATERMARK_PATH.parent.mkdir(exist_ok=True)
            wm_file.save(WATERMARK_PATH)
            logging.info("Watermark image saved to %s", WATERMARK_PATH)
            flash("תמונת סימן המים נשמרה ✔", "success")

        api_id_str = settings.get("telegram_api_id") or ""
        api_hash = settings.get("telegram_api_hash") or ""
        phone = settings.get("telegram_phone") or ""
        password = settings.get("telegram_password") or ""

        api_id = int(api_id_str) if api_id_str.isdigit() else 0

        # 1. שליחת קוד לטלגרם
        if action == "send_code":
            if not (api_id and api_hash and phone):
                flash("חייבים למלא API ID / API HASH / טלפון לפני שליחת קוד.", "danger")
                save_settings(settings)
                return redirect(url_for("settings_page"))

            try:
                logging.info("settings_page: send_code clicked")
                phone_code_hash = asyncio.run(
                    _send_telegram_code_async(api_id, api_hash, phone)
                )
                settings["telegram_phone_code_hash"] = phone_code_hash
                settings["telegram_phone_for_login"] = phone
                flash("קוד נשלח לטלגרם ✔ – הזן את הקוד בתיבה ולחץ התחברות.", "success")
            except Exception as e:
                logging.exception("Send code error")
                flash(f"שגיאה בשליחת קוד: {e}", "danger")

            save_settings(settings)
            return redirect(url_for("settings_page"))

        # 2. התחברות לטלגרם עם קוד
        if action == "login":
            code = request.form.get("telegram_code", "").strip()
            phone_code_hash = settings.get("telegram_phone_code_hash") or ""
            phone_for_login = settings.get("telegram_phone_for_login") or phone

            if not (api_id and api_hash and phone_for_login and code and phone_code_hash):
                flash(
                    "חייבים API ID, API HASH, טלפון, קוד אימות ו-phone_code_hash (לחץ קודם 'שליחת קוד').",
                    "danger",
                )
                save_settings(settings)
                return redirect(url_for("settings_page"))

            try:
                logging.info("settings_page: login clicked")
                asyncio.run(
                    _login_telegram_async(
                        api_id=api_id,
                        api_hash=api_hash,
                        phone=phone_for_login,
                        code=code,
                        password=password or None,
                        phone_code_hash=phone_code_hash,
                    )
                )
                flash("ההתחברות לטלגרם הצליחה ✔", "success")
                # מנקים את ה-hash אחרי הצלחה
                settings["telegram_phone_code_hash"] = ""
                settings["telegram_phone_for_login"] = ""
            except Exception as e:
                logging.exception("Login error")
                flash(f"שגיאה בהתחברות לטלגרם: {e}", "danger")

            save_settings(settings)
            return redirect(url_for("settings_page"))

        # 3. שמירת הגדרות רגילה
        flash("הגדרות נשמרו ✔", "success")
        save_settings(settings)
        return redirect(url_for("settings_page"))

    # GET – הצגת הדף
    return render_template("settings.html", settings=settings)


@app.route("/media/<path:filename>")
@login_required
def media(filename: str):
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/ping")
def ping():
    return "OK", 200


# --------------------------------------------------------------------------
# הפעלה לוקלית
# --------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
