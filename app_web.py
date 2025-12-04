import os
import asyncio
import logging
from pathlib import Path
from datetime import timezone

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    flash,
)
from telethon import TelegramClient, errors
from deep_translator import GoogleTranslator  # כרגע לא בשימוש, נשאר אם תרצה תרגום
from PIL import Image, ImageFilter
import requests
import json
import shutil
import subprocess
import uuid

# -------------------------------------------------
# בסיס נתיבים
# -------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
WATERMARK_PATH = DATA_DIR / "watermark.png"
SETTINGS_PATH = DATA_DIR / "settings.json"
TELEGRAM_SESSION_PATH = DATA_DIR / "telegram.session"

for p in (DATA_DIR, MEDIA_DIR):
    p.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-pasiflonet")

# סיסמת כניסה לאתר
APP_LOGIN_PASSWORD = "7447"


# -------------------------------------------------
# עזר: טעינה ושמירת הגדרות
# -------------------------------------------------


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        logging.warning("settings.json לא קיים – ייווצר קובץ ברירת מחדל")
        defaults = {
            "telegram_api_id": "",
            "telegram_api_hash": "",
            "telegram_phone": "",
            "telegram_password": "",
            "telegram_target": "",
            "telegram_phone_code_hash": "",
            "facebook_page_id": "",
            "facebook_access_token": "",
            "facebook_enabled": False,
            "auto_clean_limit": 120,
        }
        SETTINGS_PATH.write_text(
            json.dumps(defaults, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return defaults
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logging.error("load_settings: error reading settings.json: %s", e, exc_info=True)
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logging.error("save_settings: error writing settings.json: %s", e, exc_info=True)


# -------------------------------------------------
# דקורטור התחברות בסיסית לאתר
# -------------------------------------------------


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapper


# -------------------------------------------------
# עיבוד תמונה/וידיאו – טשטוש + סימן מים
# -------------------------------------------------


def apply_blur_and_watermark_image(
    src_path: Path,
    dst_path: Path,
    blur: bool,
    blur_region: dict | None,
    add_watermark: bool,
) -> None:
    """
    עיבוד תמונה עם טשטוש אזורי/מלא וסימן מים (אם קיים).
    blur_region: dict עם x, y, w, h באחוזים (0-100) יחסית לתמונה.
    """
    img = Image.open(src_path).convert("RGBA")

    if blur:
        if blur_region:
            w, h = img.size
            x = int(w * float(blur_region.get("x", 0)) / 100)
            y = int(h * float(blur_region.get("y", 0)) / 100)
            bw = int(w * float(blur_region.get("w", 100)) / 100)
            bh = int(h * float(blur_region.get("h", 100)) / 100)
            box = (x, y, min(x + bw, w), min(y + bh, h))
            crop = img.crop(box).filter(ImageFilter.GaussianBlur(radius=20))
            img.paste(crop, box)
        else:
            img = img.filter(ImageFilter.GaussianBlur(radius=20))

    if add_watermark and WATERMARK_PATH.exists():
        try:
            wm = Image.open(WATERMARK_PATH).convert("RGBA")
            # נקטין את החותמת – ~20% מרוחב התמונה
            base_w, base_h = img.size
            target_w = int(base_w * 0.2)
            ratio = target_w / wm.size[0]
            wm = wm.resize((target_w, int(wm.size[1] * ratio)), Image.LANCZOS)

            # מיקום: פינה ימנית תחתונה
            x = base_w - wm.size[0] - 10
            y = base_h - wm.size[1] - 10

            img.alpha_composite(wm, (x, y))
        except Exception as e:
            logging.error("apply_blur_and_watermark_image: watermark error: %s", e, exc_info=True)

    img = img.convert("RGB")
    img.save(dst_path, format="JPEG", quality=90)


def apply_blur_and_watermark_video(
    src_path: Path,
    dst_path: Path,
    blur: bool,
    blur_region: dict | None,
    add_watermark: bool,
) -> None:
    """
    עיבוד וידיאו עם ffmpeg – טשטוש אזורי/מלא + סימן מים.
    שימוש ב-delogo כטשטוש אזורי (זה בעצם בלר גס).
    """
    filters = []

    if blur:
        if blur_region:
            fx = float(blur_region.get("x", 0)) / 100.0
            fy = float(blur_region.get("y", 0)) / 100.0
            fw = float(blur_region.get("w", 100)) / 100.0
            fh = float(blur_region.get("h", 100)) / 100.0
            filters.append(
                f"delogo=x='iw*{fx}':y='ih*{fy}':w='iw*{fw}':h='ih*{fh}':show=0"
            )
        else:
            filters.append("boxblur=10:1")

    wm_filter = None
    if add_watermark and WATERMARK_PATH.exists():
        wm_filter = "overlay=W-w-10:H-h-10"

    vf = ""
    if filters and wm_filter:
        vf = ",".join(filters + [wm_filter])
    elif filters:
        vf = ",".join(filters)
    elif wm_filter:
        vf = wm_filter

    cmd = ["ffmpeg", "-y", "-i", str(src_path)]

    if add_watermark and WATERMARK_PATH.exists():
        cmd.extend(["-i", str(WATERMARK_PATH)])
        if vf:
            cmd.extend(["-filter_complex", vf])
        cmd.extend(["-c:v", "libx264", "-c:a", "copy", "-preset", "veryfast"])
    else:
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(["-c:v", "libx264", "-c:a", "copy", "-preset", "veryfast"])

    cmd.append(str(dst_path))

    logging.info("Running ffmpeg: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        logging.error("ffmpeg error: %s", e, exc_info=True)
        # fallback – רק העתקה
        shutil.copy(src_path, dst_path)


# -------------------------------------------------
# עזר: טלגרם
# -------------------------------------------------


async def _send_telegram_code_async(api_id: int, api_hash: str, phone: str) -> str:
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        phone_code_hash = result.phone_code_hash
        logging.info("Telegram code sent, phone_code_hash=%s", phone_code_hash)
        return phone_code_hash
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
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash or None,
            password=password or None,
        )
        logging.info("Telegram login successful")
    except errors.PhoneCodeExpiredError:
        logging.error("Telegram login: code expired")
        raise
    except errors.SessionPasswordNeededError:
        logging.error("Telegram login: 2FA password required or incorrect")
        raise
    finally:
        await client.disconnect()


async def _fetch_messages_from_all_dialogs_async(api_id: int, api_hash: str) -> list[dict]:
    """
    מחזיר עד 120 ההודעות האחרונות – הודעה אחרונה מכל דיאלוג,
    בלי GetHistory, כדי להימנע מ-FLOOD_WAIT.
    """
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        logging.warning("fetch_all_dialogs: client is not authorized")
        await client.disconnect()
        return []

    dialogs = await client.get_dialogs(limit=120)

    messages: list[dict] = []
    for d in dialogs:
        msg = d.message
        if msg is None:
            continue

        text = msg.message or ""
        dt = msg.date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        messages.append(
            {
                "dialog_title": d.name or "שיחה ללא שם",
                "text": text,
                "date": dt,
                "date_str": dt.astimezone().strftime("%Y-%m-%d %H:%M"),
                "has_media": bool(msg.media),
            }
        )

    await client.disconnect()
    messages.sort(key=lambda m: m["date"], reverse=True)
    return messages[:120]


async def _send_to_telegram_async(
    api_id: int,
    api_hash: str,
    target: str,
    text: str,
    media_path: Path | None,
) -> None:
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            logging.error("send_to_telegram: client not authorized")
            return

        if not target:
            logging.error("send_to_telegram: no target chat configured")
            return

        if media_path and media_path.exists():
            await client.send_file(target, str(media_path), caption=text or None)
        else:
            await client.send_message(target, text or "")
        logging.info("Message sent to Telegram")
    finally:
        await client.disconnect()


# -------------------------------------------------
# עזר: פייסבוק
# -------------------------------------------------


def send_to_facebook(text: str, media_path: Path | None, is_video: bool, settings: dict) -> None:
    """
    פרסום פוסט לדף פייסבוק:
    - אם יש מדיה -> /photos או /videos
    - אם אין מדיה -> /feed (פוסט טקסט בלבד)
    """
    page_id = (settings.get("facebook_page_id") or "").strip()
    access_token = (settings.get("facebook_access_token") or "").strip()
    enabled = settings.get("facebook_enabled", False)

    if not (enabled and page_id and access_token):
        logging.info("Facebook posting skipped (disabled or missing config).")
        return

    base_url = f"https://graph.facebook.com/v18.0/{page_id}"

    try:
        if media_path is not None and media_path.exists():
            files = {"source": open(media_path, "rb")}
            data = {
                "access_token": access_token,
            }
            if is_video:
                data["description"] = text or ""
                endpoint = "/videos"
            else:
                data["caption"] = text or ""
                endpoint = "/photos"

            resp = requests.post(base_url + endpoint, data=data, files=files, timeout=30)
            logging.info("Facebook media post status %s: %s", resp.status_code, resp.text[:200])
        else:
            data = {
                "access_token": access_token,
                "message": text or "",
            }
            resp = requests.post(base_url + "/feed", data=data, timeout=30)
            logging.info("Facebook text post status %s: %s", resp.status_code, resp.text[:200])

    except Exception as e:
        logging.error("Error sending to Facebook: %s", e, exc_info=True)


def auto_clean_media_and_messages(limit: int = 120) -> None:
    """
    ניקוי אוטומטי של קבצי מדיה ישנים – אם יש יותר מ-limit קבצים.
    (ניקוי הודעות בטבלאות נעשה ברמת DB / JSON אם יש – כאן מטפלים רק במדיה.)
    """
    try:
        files = sorted(
            [p for p in MEDIA_DIR.glob("*") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
        )
        if len(files) <= limit:
            return
        to_delete = files[0 : len(files) - limit]
        for f in to_delete:
            try:
                f.unlink()
            except Exception:
                pass
        logging.info("auto_clean_media: deleted %d old files", len(to_delete))
    except Exception as e:
        logging.error("auto_clean_media: %s", e, exc_info=True)


# -------------------------------------------------
# ראוטים
# -------------------------------------------------


@app.route("/")
def index():
    if session.get("logged_in"):
        return redirect(url_for("messages"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == APP_LOGIN_PASSWORD:
            session["logged_in"] = True
            flash("התחברת בהצלחה", "success")
            return redirect(url_for("messages"))
        else:
            flash("סיסמה שגויה", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/messages")
@login_required
def messages():
    settings = load_settings()
    api_id = (settings.get("telegram_api_id") or "").strip()
    api_hash = (settings.get("telegram_api_hash") or "").strip()

    telegram_connected = TELEGRAM_SESSION_PATH.exists()
    messages_list: list[dict] = []

    if api_id and api_hash and telegram_connected:
        try:
            messages_list = asyncio.run(
                _fetch_messages_from_all_dialogs_async(int(api_id), api_hash)
            )
        except Exception as e:
            logging.error("messages: error fetching from Telegram: %s", e, exc_info=True)
            flash("שגיאה בטעינת הודעות מטלגרם", "danger")
    else:
        logging.info("messages: Telegram not configured or no session file")

    return render_template(
        "messages.html",
        messages=messages_list,
        telegram_connected=telegram_connected,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    settings = load_settings()

    if request.method == "POST":
        logging.info("settings_page: POST data keys=%s", list(request.form.keys()))

        # עדכון שדות בסיס
        settings["telegram_api_id"] = (request.form.get("telegram_api_id") or "").strip()
        settings["telegram_api_hash"] = (request.form.get("telegram_api_hash") or "").strip()
        settings["telegram_phone"] = (request.form.get("telegram_phone") or "").strip()
        settings["telegram_password"] = (request.form.get("telegram_password") or "").strip()
        settings["telegram_target"] = (request.form.get("telegram_target") or "").strip()

        settings["facebook_page_id"] = (request.form.get("facebook_page_id") or "").strip()
        settings["facebook_access_token"] = (request.form.get("facebook_access_token") or "").strip()
        settings["facebook_enabled"] = bool(request.form.get("facebook_enabled"))

        # watermark upload
        if "watermark" in request.files:
            file = request.files["watermark"]
            if file and file.filename:
                WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
                file.save(WATERMARK_PATH)
                logging.info("Watermark image saved to %s", WATERMARK_PATH)
                flash("סימן המים עודכן", "success")

        api_id = settings.get("telegram_api_id")
        api_hash = settings.get("telegram_api_hash")
        phone = settings.get("telegram_phone")
        password = settings.get("telegram_password") or ""

        # כפתור שליחת קוד
        if "send_code" in request.form:
            logging.info("settings_page: send_code clicked")
            if not (api_id and api_hash and phone):
                flash("נא למלא API ID, API HASH וטלפון", "danger")
            else:
                try:
                    phone_code_hash = asyncio.run(
                        _send_telegram_code_async(int(api_id), api_hash, phone)
                    )
                    settings["telegram_phone_code_hash"] = phone_code_hash
                    save_settings(settings)
                    flash("קוד נשלח לטלגרם ✔", "success")
                except Exception as e:
                    logging.error("settings_page: send_code error: %s", e, exc_info=True)
                    flash(f"שגיאה בשליחת קוד: {e}", "danger")

        # כפתור התחברות לטלגרם
        elif "login_telegram" in request.form:
            logging.info("settings_page: login clicked")
            code = (request.form.get("telegram_code") or "").strip()
            phone_code_hash = settings.get("telegram_phone_code_hash") or ""
            if not code:
                flash("נא למלא קוד שהגיע בטלגרם", "danger")
            elif not phone_code_hash:
                flash("אין קוד אימות שנשמר – לחץ קודם על 'שליחת קוד'", "danger")
            elif not (api_id and api_hash and phone):
                flash("נא למלא API ID, API HASH וטלפון", "danger")
            else:
                try:
                    asyncio.run(
                        _login_telegram_async(
                            int(api_id),
                            api_hash,
                            phone,
                            code,
                            password or None,
                            phone_code_hash,
                        )
                    )
                    flash("התחברות לטלגרם הצליחה ✔", "success")
                except errors.PhoneCodeExpiredError:
                    flash("קוד האימות פג תוקף – לחץ שוב 'שליחת קוד' והשתמש בקוד האחרון שמגיע", "danger")
                except errors.SessionPasswordNeededError:
                    flash("נדרש סיסמת אימות דו-שלבי (2FA) או שהסיסמה שגויה", "danger")
                except Exception as e:
                    logging.error("settings_page: login error: %s", e, exc_info=True)
                    flash(f"שגיאת התחברות: {e}", "danger")

        # שמירת הגדרות רגילה
        else:
            save_settings(settings)
            flash("ההגדרות נשמרו", "success")

        save_settings(settings)
        return redirect(url_for("settings_page"))

    return render_template("settings.html", settings=settings)


@app.route("/new", methods=["GET", "POST"])
@login_required
def new_message():
    settings = load_settings()
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        apply_blur = bool(request.form.get("apply_blur"))
        apply_watermark = bool(request.form.get("apply_watermark"))

        # שדות טשטוש אזורי באחוזים (אם קיימים בטופס)
        blur_region = None
        try:
            bx = request.form.get("blur_x")
            by = request.form.get("blur_y")
            bw = request.form.get("blur_w")
            bh = request.form.get("blur_h")
            if bx is not None and by is not None and bw is not None and bh is not None:
                blur_region = {
                    "x": float(bx),
                    "y": float(by),
                    "w": float(bw),
                    "h": float(bh),
                }
        except Exception:
            blur_region = None

        upload = request.files.get("media")
        media_path = None
        processed_path = None
        is_video = False

        if upload and upload.filename:
            ext = os.path.splitext(upload.filename)[1].lower()
            uid = uuid.uuid4().hex
            media_path = MEDIA_DIR / f"orig_{uid}{ext}"
            upload.save(media_path)

            # נשמור את הקובץ המעובד כ-jpg לסטילס ו-mp4 לוידיאו
            if ext in [".mp4", ".mov", ".mkv", ".avi"]:
                is_video = True
                processed_path = MEDIA_DIR / f"proc_{uid}.mp4"
                apply_blur_and_watermark_video(
                    media_path,
                    processed_path,
                    blur=apply_blur,
                    blur_region=blur_region,
                    add_watermark=apply_watermark,
                )
            else:
                is_video = False
                processed_path = MEDIA_DIR / f"proc_{uid}.jpg"
                apply_blur_and_watermark_image(
                    media_path,
                    processed_path,
                    blur=apply_blur,
                    blur_region=blur_region,
                    add_watermark=apply_watermark,
                )

        # שליחה לטלגרם
        api_id = (settings.get("telegram_api_id") or "").strip()
        api_hash = (settings.get("telegram_api_hash") or "").strip()
        target = (settings.get("telegram_target") or "").strip()

        if api_id and api_hash and TELEGRAM_SESSION_PATH.exists():
            try:
                asyncio.run(
                    _send_to_telegram_async(
                        int(api_id),
                        api_hash,
                        target,
                        text,
                        processed_path or media_path,
                    )
                )
            except Exception as e:
                logging.error("new_message: telegram send error: %s", e, exc_info=True)
                flash("שגיאה בשליחה לטלגרם", "danger")
        else:
            logging.info("new_message: telegram not configured or not logged in")

        # שליחה לפייסבוק (אם הופעל)
        try:
            send_to_facebook(text, processed_path or media_path, is_video, settings)
        except Exception as e:
            logging.error("new_message: facebook send error: %s", e, exc_info=True)

        # ניקוי אוטומטי של מדיה ישנה
        limit = int(settings.get("auto_clean_limit") or 120)
        auto_clean_media_and_messages(limit=limit)

        flash("ההודעה נשלחה (טלגרם / פייסבוק אם הופעל)", "success")
        return redirect(url_for("messages"))

    return render_template("new_message.html", settings=settings)


@app.route("/media/<path:filename>")
@login_required
def media(filename: str):
    return send_from_directory(MEDIA_DIR, filename)


if __name__ == "__main__":
    # להרצה מקומית
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
