from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from telethon import TelegramClient
from telethon.errors import PhoneCodeExpiredError
from deep_translator import GoogleTranslator
from PIL import Image, ImageFilter

# -----------------------------------------------------------------------------
# הגדרות בסיסיות
# -----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
MESSAGES_PATH = DATA_DIR / "messages.json"
SESSION_PATH = DATA_DIR / "telegram_session"
WATERMARK_PATH = DATA_DIR / "watermark.png"

MAX_MESSAGES = 120
APP_PASSWORD = os.getenv("APP_PASSWORD", "7447")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-CHANGE-ME")


# -----------------------------------------------------------------------------
# עוזרים כלליים
# -----------------------------------------------------------------------------

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("app_authed"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


def load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load settings.json")
        return {}


def save_settings(settings: Dict[str, Any]) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Failed to save settings.json")


def load_messages() -> List[Dict[str, Any]]:
    if not MESSAGES_PATH.exists():
        return []
    try:
        with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception("Failed to load messages.json")
        return []


def save_messages(msgs: List[Dict[str, Any]]) -> None:
    try:
        with open(MESSAGES_PATH, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Failed to save messages.json")


def cleanup_old_messages() -> None:
    msgs = load_messages()
    if len(msgs) <= MAX_MESSAGES:
        return

    msgs_sorted = sorted(
        msgs,
        key=lambda m: m.get("created_at", ""),
    )
    to_delete = msgs_sorted[:-MAX_MESSAGES]
    remaining = msgs_sorted[-MAX_MESSAGES:]

    for m in to_delete:
        fn = m.get("media_filename")
        if fn:
            fp = MEDIA_DIR / fn
            if fp.exists():
                try:
                    fp.unlink()
                except Exception:
                    logging.exception("Failed to delete old media file %s", fp)

    save_messages(remaining)


def translate_to_hebrew(text: str) -> str:
    """תרגום מהיר לעברית (אם יש צורך)."""
    text = (text or "").strip()
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target="he").translate(text)
    except Exception:
        logging.exception("Translation failed")
        return text


# -----------------------------------------------------------------------------
# עיבוד מדיה – תמונות / וידאו
# -----------------------------------------------------------------------------

def _apply_blur_and_watermark_image(
    src_path: Path,
    dst_path: Path,
    blur_rect: Optional[Dict[str, float]] = None,
    watermark_path: Optional[Path] = None,
) -> None:
    """
    טשטוש אזורי + סימן מים לתמונה.
    blur_rect: {x, y, w, h} כיחס (0..1) לרוחב/גובה.
    """
    with Image.open(src_path).convert("RGBA") as im:
        w, h = im.size
        base = im.copy()

        # טשטוש אזורי
        if blur_rect:
            bx = int(blur_rect.get("x", 0) * w)
            by = int(blur_rect.get("y", 0) * h)
            bw = int(blur_rect.get("w", 1) * w)
            bh = int(blur_rect.get("h", 1) * h)

            bx = max(0, bx)
            by = max(0, by)
            bw = max(1, bw)
            bh = max(1, bh)
            if bx + bw > w:
                bw = w - bx
            if by + bh > h:
                bh = h - by

            region = base.crop((bx, by, bx + bw, by + bh))
            blurred = region.filter(ImageFilter.GaussianBlur(radius=24))
            base.paste(blurred, (bx, by))

        # סימן מים
        if watermark_path and watermark_path.exists():
            try:
                with Image.open(watermark_path).convert("RGBA") as wm:
                    wm_w, wm_h = wm.size
                    scale = min(w / (4 * wm_w), h / (4 * wm_h), 1.0)
                    if scale != 1.0:
                        wm = wm.resize(
                            (int(wm_w * scale), int(wm_h * scale)),
                            Image.LANCZOS,
                        )
                    margin = int(min(w, h) * 0.03)
                    pos = (w - wm.width - margin, h - wm.height - margin)

                    overlay = Image.new("RGBA", base.size)
                    overlay.paste(wm, pos, wm)
                    base = Image.alpha_composite(base, overlay)
            except Exception:
                logging.exception("Failed to apply watermark")

        base.convert("RGB").save(dst_path, quality=90)


def _process_image_upload(
    upload,
    blur_rect: Optional[Dict[str, float]],
    watermark_path: Optional[Path],
) -> str:
    ext = Path(upload.filename).suffix.lower() or ".jpg"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    raw_name = f"img_{ts}_raw{ext}"
    final_name = f"img_{ts}{ext}"

    raw_path = MEDIA_DIR / raw_name
    dst_path = MEDIA_DIR / final_name

    upload.save(raw_path)
    _apply_blur_and_watermark_image(raw_path, dst_path, blur_rect, watermark_path)

    try:
        raw_path.unlink()
    except Exception:
        pass

    return final_name


def _process_video_upload(upload, watermark_path: Optional[Path]) -> str:
    """
    שמירת וידאו. אופציונלית – אפשר להוסיף כאן FFMPEG לסימן מים / טשטוש.
    כרגע: שמירה כמו שהוא, כדי לא להסתבך עם ffmpeg על שרתים שונים.
    """
    ext = Path(upload.filename).suffix.lower() or ".mp4"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    final_name = f"vid_{ts}{ext}"
    dst_path = MEDIA_DIR / final_name
    upload.save(dst_path)

    # אם תרצה לשלב ffmpeg, כאן המקום:
    # cmd = [FFMPEG_BIN, "-i", str(dst_path), ...]
    # subprocess.run(cmd, check=True)

    return final_name


# -----------------------------------------------------------------------------
# טלגרם – שליחת קוד / התחברות / שליחת הודעות
# -----------------------------------------------------------------------------

async def _send_code_async(api_id: int, api_hash: str, phone: str) -> None:
    """
    שליחת קוד לטלגרם ושמירת phone_code_hash ב-settings.
    """
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        settings = load_settings()
        settings["telegram_phone_code_hash"] = result.phone_code_hash
        settings["telegram_phone_for_login"] = phone
        save_settings(settings)
        logging.info("Telegram code sent and phone_code_hash saved")
    finally:
        await client.disconnect()


async def _login_telegram_async(
    api_id: int,
    api_hash: str,
    phone: str,
    code: str,
    password: Optional[str],
    phone_code_hash: str,
) -> None:
    """
    התחברות עם קוד + phone_code_hash.
    """
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(
            phone=phone,
            code=code,
            password=password or None,
            phone_code_hash=phone_code_hash,
        )
        logging.info("Telegram login OK")
    finally:
        await client.disconnect()


def _get_telegram_client_from_settings() -> Optional[TelegramClient]:
    """
    מחזיר אובייקט טלגרם מחובר לפי ה-session שנשמר.
    אם אין session – מחזיר None.
    """
    settings = load_settings()
    api_id_str = settings.get("telegram_api_id") or ""
    api_hash = settings.get("telegram_api_hash") or ""
    if not (api_id_str and api_hash and SESSION_PATH.exists()):
        return None
    try:
        api_id = int(api_id_str)
    except ValueError:
        return None

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    return client


async def _send_telegram_message_async(
    text: str,
    target: str,
    media_path: Optional[Path] = None,
) -> None:
    settings = load_settings()
    api_id_str = settings.get("telegram_api_id") or ""
    api_hash = settings.get("telegram_api_hash") or ""
    if not (api_id_str and api_hash and SESSION_PATH.exists()):
        logging.warning("Telegram client not configured or session missing")
        return
    try:
        api_id = int(api_id_str)
    except ValueError:
        logging.warning("Telegram API ID is not numeric")
        return

    client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        if media_path and media_path.exists():
            await client.send_file(target, file=str(media_path), caption=text or None)
        else:
            await client.send_message(target, text)
    finally:
        await client.disconnect()


# -----------------------------------------------------------------------------
# ראוטים
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    if not session.get("app_authed"):
        return redirect(url_for("login"))
    return redirect(url_for("messages"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pwd = (request.form.get("password") or "").strip()
        if pwd == APP_PASSWORD:
            session["app_authed"] = True
            flash("ברוך הבא לפסיפלונט Web ✔", "success")
            return redirect(url_for("messages"))
        else:
            flash("סיסמה שגויה", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("התנתקת מהמערכת", "info")
    return redirect(url_for("login"))


@app.route("/messages")
@login_required
def messages():
    msgs = load_messages()
    msgs_sorted = sorted(
        msgs,
        key=lambda m: m.get("created_at", ""),
        reverse=True,
    )
    return render_template("messages.html", messages=msgs_sorted)


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_message():
    settings = load_settings()
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        translate = request.form.get("translate_to_he", "off") == "on"
        send_telegram = request.form.get("send_telegram", "off") == "on"
        send_facebook = request.form.get("send_facebook", "off") == "on"

        if translate:
            text = translate_to_hebrew(text)

        # נתוני טשטוש (מהקנבס עם ג'ויסטיקים) – יחסי 0..1
        try:
            blur_x = float(request.form.get("blur_x", "0"))
            blur_y = float(request.form.get("blur_y", "0"))
            blur_w = float(request.form.get("blur_w", "1"))
            blur_h = float(request.form.get("blur_h", "1"))
            blur_rect = {"x": blur_x, "y": blur_y, "w": blur_w, "h": blur_h}
        except ValueError:
            blur_rect = None

        media_file = request.files.get("media")
        media_filename = None
        media_type = None

        if media_file and media_file.filename:
            ext = Path(media_file.filename).suffix.lower()
            if ext in {".jpg", ".jpeg", ".png", ".webp"}:
                media_type = "image"
                media_filename = _process_image_upload(
                    media_file,
                    blur_rect=blur_rect,
                    watermark_path=WATERMARK_PATH if WATERMARK_PATH.exists() else None,
                )
            elif ext in {".mp4", ".mov", ".mkv", ".avi"}:
                media_type = "video"
                media_filename = _process_video_upload(
                    media_file,
                    watermark_path=WATERMARK_PATH if WATERMARK_PATH.exists() else None,
                )
            else:
                flash("פורמט קובץ לא נתמך", "danger")

        # שמירת הודעה ברשימה
        msgs = load_messages()
        now = datetime.utcnow().isoformat(timespec="seconds")
        msg = {
            "id": len(msgs) + 1,
            "text": text,
            "created_at": now,
            "media_filename": media_filename,
            "media_type": media_type,
            "send_telegram": send_telegram,
            "send_facebook": send_facebook,
        }
        msgs.append(msg)
        save_messages(msgs)
        cleanup_old_messages()

        # שליחה לטלגרם
        if send_telegram:
            target = settings.get("telegram_target") or ""
            if not target:
                flash("לא הוגדר יעד טלגרם בהגדרות", "warning")
            else:
                try:
                    media_path = (
                        MEDIA_DIR / media_filename
                        if media_filename
                        else None
                    )
                    asyncio.run(
                        _send_telegram_message_async(
                            text=text,
                            target=target,
                            media_path=media_path,
                        )
                    )
                    flash("ההודעה נשלחה לטלגרם ✔", "success")
                except Exception as e:
                    logging.exception("Failed to send Telegram message")
                    flash(f"שגיאה בשליחה לטלגרם: {e}", "danger")

        # TODO: שליחה לפייסבוק – ניתן להוסיף כאן לפי Graph API

        flash("ההודעה נשמרה במערכת ✔", "success")
        return redirect(url_for("messages"))

    return render_template("new.html")


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    settings = load_settings()

    if request.method == "POST":
        action = request.form.get("action", "save")

        # שדות טלגרם
        api_id_str = (request.form.get("telegram_api_id") or "").strip()
        api_hash = (request.form.get("telegram_api_hash") or "").strip()
        phone = (request.form.get("telegram_phone") or "").strip()
        password = (request.form.get("telegram_password") or "").strip()
        target = (request.form.get("telegram_target") or "").strip()
        sources = (request.form.get("telegram_sources") or "").strip()

        settings["telegram_api_id"] = api_id_str
        settings["telegram_api_hash"] = api_hash
        settings["telegram_phone"] = phone
        settings["telegram_password"] = password
        settings["telegram_target"] = target
        settings["telegram_sources"] = sources

        # פייסבוק (אם קיימים שדות בטופס)
        fb_token = (request.form.get("facebook_page_token") or "").strip()
        fb_page_id = (request.form.get("facebook_page_id") or "").strip()
        if fb_token:
            settings["facebook_page_token"] = fb_token
        if fb_page_id:
            settings["facebook_page_id"] = fb_page_id

        # העלאת סימן מים (תמונה)
        wm_file = request.files.get("watermark_image")
        if wm_file and wm_file.filename:
            try:
                WATERMARK_PATH.parent.mkdir(exist_ok=True)
                tmp_path = WATERMARK_PATH.with_suffix(".tmp")
                wm_file.save(tmp_path)
                # נוודא שזו תמונה חוקית
                with Image.open(tmp_path) as _im:
                    _im.verify()
                shutil.move(tmp_path, WATERMARK_PATH)
                flash("תמונת סימן מים נשמרה ✔", "success")
            except Exception as e:
                logging.exception("Failed to save watermark image")
                flash(f"שגיאה בשמירת סימן מים: {e}", "danger")

        # נסה להמיר ל-int
        api_id: Optional[int] = None
        if api_id_str:
            try:
                api_id = int(api_id_str)
            except ValueError:
                flash("API ID חייב להיות מספרי", "danger")

        # שליחת קוד
        if action == "send_code":
            if not (api_id and api_hash and phone):
                flash("צריך למלא API ID, API Hash וטלפון לפני שליחת קוד", "danger")
            else:
                try:
                    asyncio.run(_send_code_async(api_id, api_hash, phone))
                    flash("קוד נשלח לטלגרם ✔", "success")
                except Exception as e:
                    logging.exception("Send code error")
                    flash(f"שגיאה בשליחת קוד: {e}", "danger")

            save_settings(settings)
            return redirect(url_for("settings_page"))

        # התחברות
        if action == "login":
            code = (request.form.get("login_code") or "").strip()
            if not code:
                flash("צריך למלא את קוד האימות שקיבלת בטלגרם", "danger")
                save_settings(settings)
                return redirect(url_for("settings_page"))

            phone_code_hash = settings.get("telegram_phone_code_hash")
            phone_for_login = settings.get("telegram_phone_for_login") or phone

            if not phone_code_hash:
                flash("אין hash של קוד. לחץ שוב על 'שליחת קוד' והשתמש בקוד האחרון שמגיע.", "danger")
                save_settings(settings)
                return redirect(url_for("settings_page"))

            if not (api_id and api_hash and phone_for_login):
                flash("חסרים נתוני API או טלפון. מלא שוב את השדות.", "danger")
                save_settings(settings)
                return redirect(url_for("settings_page"))

            try:
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
                flash("התחברות לטלגרם הצליחה ✔", "success")
            except PhoneCodeExpiredError:
                flash("קוד האימות פג תוקף – שלח שוב קוד והשתמש בקוד האחרון שמגיע.", "danger")
            except Exception as e:
                logging.exception("Login error")
                flash(f"שגיאה בהתחברות לטלגרם: {e}", "danger")

            save_settings(settings)
            return redirect(url_for("settings_page"))

        # ברירת מחדל – רק שמירת הגדרות
        save_settings(settings)
        flash("ההגדרות נשמרו ✔", "success")
        return redirect(url_for("settings_page"))

    watermark_exists = WATERMARK_PATH.exists()
    return render_template(
        "settings.html",
        settings=settings,
        watermark_exists=watermark_exists,
    )


@app.route("/media/<path:filename>")
@login_required
def media_file(filename: str):
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/favicon.ico")
def favicon():
    # אם יש favicon בתיקיית static
    fav_path = BASE_DIR / "static" / "favicon.ico"
    if fav_path.exists():
        return send_from_directory(BASE_DIR / "static", "favicon.ico")
    # אחרת 204 "שקט"
    return ("", 204)


if __name__ == "__main__":
    # הרצה מקומית
    app.run(host="0.0.0.0", port=5000, debug=True)
