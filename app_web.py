import os
import json
import logging
import asyncio
import subprocess
from datetime import datetime
from typing import Optional, List, Dict

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
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
)
from deep_translator import GoogleTranslator
from PIL import Image, ImageFilter, ImageDraw, ImageFont

# -------------------------------------------------------------------
# הגדרות בסיס
# -------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
APP_DATA_DIR = DATA_DIR
TEMP_DIR = os.path.join(DATA_DIR, "temp")
MEDIA_DIR = os.path.join(DATA_DIR, "media")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
MESSAGES_PATH = os.path.join(DATA_DIR, "messages.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# -------------------------------------------------------------------
# גלובלים
# -------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = "pasiflonet-secret-key"

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

tg_client: Optional[TelegramClient] = None
login_client: Optional[TelegramClient] = None

MESSAGES: List[Dict] = []

MAX_MESSAGES = 120  # ניקוי אוטומטי אחרי 120 הודעות


# -------------------------------------------------------------------
# עזרה: הגדרות
# -------------------------------------------------------------------

def load_settings() -> Dict:
    if not os.path.exists(SETTINGS_PATH):
        logging.warning("settings.json לא קיים – ייווצר קובץ ברירת מחדל")
        data = {
            "api_id": "",
            "api_hash": "",
            "phone": "",
            "default_channel": "me",
            "signature_text": "",
            "facebook_page_access_token": "",
            "facebook_page_id": "",
            "watermark_image": "",
            "session": "",
            "admin_password": "",
        }
        save_settings(data)
        return data

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"failed to load settings: {e}")
        return {}


def save_settings(data: Dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


settings = load_settings()


# -------------------------------------------------------------------
# עזרה: הודעות מקומיות + ניקוי אוטומטי
# -------------------------------------------------------------------

def load_messages() -> None:
    global MESSAGES
    if not os.path.exists(MESSAGES_PATH):
        MESSAGES = []
        return
    try:
        with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
            MESSAGES = json.load(f)
    except Exception as e:
        logging.error(f"failed to load messages: {e}")
        MESSAGES = []


def save_messages() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MESSAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(MESSAGES, f, ensure_ascii=False, indent=2)


def trim_old_messages() -> None:
    """
    משאיר רק MAX_MESSAGES הודעות אחרונות ומוחק מדיה ישנה מהדיסק.
    """
    global MESSAGES
    if len(MESSAGES) <= MAX_MESSAGES:
        return

    to_delete = MESSAGES[:-MAX_MESSAGES]
    keep = MESSAGES[-MAX_MESSAGES:]

    for msg in to_delete:
        for key in ("media_path", "media_original", "media_preview"):
            path = msg.get(key)
            if path:
                if not os.path.isabs(path):
                    full = os.path.join(DATA_DIR, path)
                else:
                    full = path
                try:
                    if os.path.exists(full):
                        os.remove(full)
                        logging.info(f"Deleted old media file: {full}")
                except Exception as e:
                    logging.error(f"Failed deleting media {full}: {e}")

    MESSAGES = keep
    save_messages()
    logging.info("trim_old_messages: ניקוי אוטומטי בוצע")


load_messages()


# -------------------------------------------------------------------
# עזרה: FFmpeg / Watermark
# -------------------------------------------------------------------

def get_ffmpeg_path() -> str:
    """
    החזר נתיב ל-ffmpeg. אם ffmpeg/ffmpeg.exe קיים בתיקיה – השתמש בו.
    אחרת נניח ש-ffmpeg במערכת.
    """
    local_ffmpeg = os.path.join(BASE_DIR, "ffmpeg", "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        return local_ffmpeg
    return "ffmpeg"


def get_watermark_path() -> Optional[str]:
    name = settings.get("watermark_image") or ""
    if not name:
        return None
    p = os.path.join(APP_DATA_DIR, name)
    return p if os.path.exists(p) else None


# -------------------------------------------------------------------
# עזרה: טלגרם
# -------------------------------------------------------------------

async def ensure_tg_client():
    global tg_client, settings

    if tg_client and tg_client.is_connected():
        return

    session_str = settings.get("session", "")
    api_id = settings.get("api_id")
    api_hash = settings.get("api_hash")

    if not api_id or not api_hash or not session_str:
        raise RuntimeError("אין הגדרות טלגרם / SESSION – התחבר דרך ההגדרות")

    try:
        api_id_int = int(api_id)
    except ValueError:
        raise RuntimeError("API ID חייב להיות מספר")

    client = TelegramClient(
        StringSession(session_str),
        api_id_int,
        api_hash,
        loop=loop
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("המשתמש לא מחובר – צריך להתחבר מחדש דרך ההגדרות")

    tg_client = client
    logging.info("Telegram client is ready")


# -------------------------------------------------------------------
# עיבוד תמונה – טשטוש אזורי + לוגו
# -------------------------------------------------------------------

def process_image_blur_and_watermark(
    input_path: str,
    blur: bool = True,
    add_wm: bool = True,
    blur_rect=None,   # (x, y, w, h)
    wm_rect=None,     # (cx, cy, size)
) -> str:
    if not os.path.exists(input_path):
        logging.error(f"image not found for processing: {input_path}")
        return input_path

    try:
        im = Image.open(input_path).convert("RGBA")
    except Exception as e:
        logging.error(f"Failed to open image: {e}")
        return input_path

    w, h = im.size

    # טשטוש רק אם יש מלבן מוגדר – אין fallback לטשטוש מלא
    if blur and blur_rect:
        try:
            bx, by, bw, bh = blur_rect
            bx = max(0, min(bx, w - 1))
            by = max(0, min(by, h - 1))
            bw = max(1, min(bw, w - bx))
            bh = max(1, min(bh, h - by))
            region = im.crop((bx, by, bx + bw, by + bh))
            region = region.filter(ImageFilter.GaussianBlur(radius=18))
            im.paste(region, (bx, by))
        except Exception as e:
            logging.error(f"Partial blur failed: {e}")

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if add_wm:
        wm_path = get_watermark_path()
        if wm_path and os.path.exists(wm_path):
            try:
                wm = Image.open(wm_path).convert("RGBA")
                if wm_rect:
                    cx, cy, size = wm_rect
                    size = max(10, size)
                    ratio = size / max(wm.width, wm.height)
                    new_w = int(wm.width * ratio)
                    new_h = int(wm.height * ratio)
                    wm = wm.resize((new_w, new_h), Image.LANCZOS)
                    x = int(cx - new_w / 2)
                    y = int(cy - new_h / 2)
                    x = max(0, min(x, w - new_w))
                    y = max(0, min(y, h - new_h))
                else:
                    max_w = int(w * 0.25)
                    ratio = max_w / wm.width
                    wm = wm.resize((max_w, int(wm.height * ratio)), Image.LANCZOS)
                    new_w, new_h = wm.size
                    x = w - new_w - 20
                    y = h - new_h - 20

                alpha = wm.split()[3].point(lambda p: int(p * 0.85))
                wm.putalpha(alpha)
                overlay.paste(wm, (x, y), wm)
            except Exception as e:
                logging.error(f"Failed to apply image watermark: {e}")
        else:
            txt = "PASIFLONET"
            try:
                font = ImageFont.truetype("arial.ttf", 20)
            except Exception:
                font = ImageFont.load_default()
            tw, th = draw.textsize(txt, font=font)
            x = w - tw - 20
            y = h - th - 20
            draw.text((x, y), txt, font=font, fill=(57, 255, 20, 180))

    out = Image.alpha_composite(im, overlay)
    out_path = os.path.join(TEMP_DIR, f"blurwm_{os.path.basename(input_path)}")
    out.convert("RGB").save(out_path, "JPEG", quality=90)
    logging.info(f"Processed image saved to {out_path}")
    return out_path


# -------------------------------------------------------------------
# עיבוד וידיאו – טשטוש אזורי + לוגו
# -------------------------------------------------------------------

def process_video_blur_and_watermark(
    input_path: str,
    blur: bool = True,
    add_wm: bool = True,
    blur_rect=None,
    wm_rect=None,
) -> str:
    if not os.path.exists(input_path):
        logging.error(f"video not found for processing: {input_path}")
        return input_path

    ffmpeg_bin = get_ffmpeg_path()
    out_path = os.path.join(TEMP_DIR, f"blurwm_{os.path.basename(input_path)}")

    wm_path = get_watermark_path() if add_wm else None
    if wm_path and not os.path.exists(wm_path):
        wm_path = None

    if not blur and not wm_path:
        return input_path

    have_blur = bool(blur and blur_rect)

    if have_blur:
        bx, by, bw, bh = blur_rect
        bx = max(0, int(bx))
        by = max(0, int(by))
        bw = max(1, int(bw))
        bh = max(1, int(bh))
    else:
        bx = by = bw = bh = 0

    have_wm = bool(wm_path and wm_rect)
    if have_wm:
        wx, wy, wsize = wm_rect
        wsize = max(16, int(wsize or 0))
        wx = int(wx or 0)
        wy = int(wy or 0)
        wmx = wx - wsize // 2
        wmy = wy - wsize // 2
        wmx = max(0, wmx)
        wmy = max(0, wmy)
    else:
        wsize = 0
        wmx = wmy = 0

    filter_parts = []

    if have_blur:
        filter_parts.append(
            f"[0:v]split=2[main][blur];"
            f"[blur]crop=w={bw}:h={bh}:x={bx}:y={by},gblur=sigma=20[blurred];"
            f"[main][blurred]overlay=x={bx}:y={by}[v1]"
        )
        current_label = "v1"
    else:
        filter_parts.append("[0:v]null[v1]")
        current_label = "v1"

    if have_wm:
        filter_parts.append(
            f"[1:v]scale={wsize}:-1[wm];"
            f"[{current_label}][wm]overlay=x={wmx}:y={wmy}[vout]"
        )
        video_label = "[vout]"
    else:
        video_label = f"[{current_label}]"

    filter_complex = ";".join(filter_parts)

    cmd = [ffmpeg_bin, "-y", "-i", input_path]
    if have_wm:
        cmd += ["-i", wm_path]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", video_label,
        "-map", "0:a?", "-c:a", "copy",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        out_path,
    ]

    try:
        logging.info(f"Running FFmpeg (video blur+wm): {' '.join(cmd)}")
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        logging.info(f"Processed video saved to {out_path}")
        return out_path
    except Exception as e:
        logging.error(f"FFmpeg video processing failed: {e}")
        return input_path


# -------------------------------------------------------------------
# פלאסקים – ראוטים
# -------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("messages_list"))


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(DATA_DIR, filename)


@app.route("/messages")
def messages_list():
    sorted_msgs = sorted(
        MESSAGES,
        key=lambda m: m.get("timestamp", ""),
        reverse=True,
    )
    return render_template(
        "messages.html",
        messages=sorted_msgs,
        settings=settings,
        MAX_MESSAGES=MAX_MESSAGES,
    )


@app.route("/messages/<int:msg_id>", methods=["GET", "POST"])
def message_detail(msg_id):
    msg = next((m for m in MESSAGES if m["message_id"] == msg_id), None)
    if not msg:
        flash("הודעה לא נמצאה", "danger")
        return redirect(url_for("messages_list"))

    if request.method == "POST":
        send_to_telegram = bool(request.form.get("send_tg"))
        send_to_facebook = bool(request.form.get("send_fb"))
        translate_to_he = bool(request.form.get("translate_he"))
        blur_media = bool(request.form.get("blur_media"))
        add_watermark = bool(request.form.get("add_watermark"))

        text_to_send = request.form.get("text", "") or ""
        signature = settings.get("signature_text", "")
        if signature:
            text_to_send = f"{text_to_send}\n{signature}".strip()

        if translate_to_he and text_to_send:
            try:
                translated = GoogleTranslator(source="auto", target="iw").translate(text_to_send)
                text_to_send = translated
            except Exception as e:
                logging.error(f"Translation failed: {e}")
                flash(f"שגיאה בתרגום: {e}", "danger")

        try:
            future = asyncio.run_coroutine_threadsafe(ensure_tg_client(), loop)
            future.result(timeout=30)
        except Exception as e:
            logging.error(f"Telegram init error: {e}")
            flash(str(e), "danger")
            return redirect(url_for("message_detail", msg_id=msg_id))

        async def do_send():
            global tg_client
            target = settings.get("default_channel", "me")
            media_path = msg.get("media_path")
            media_type = msg.get("media_type")

            final_media_path = media_path

            blur_rect = None
            wm_rect = None

            try:
                bx = int(request.form.get("blur_x") or 0)
                by = int(request.form.get("blur_y") or 0)
                bw = int(request.form.get("blur_w") or 0)
                bh = int(request.form.get("blur_h") or 0)
                if bw > 0 and bh > 0:
                    blur_rect = (bx, by, bw, bh)
            except ValueError:
                blur_rect = None

            try:
                wx = int(request.form.get("wm_x") or 0)
                wy = int(request.form.get("wm_y") or 0)
                ws = int(request.form.get("wm_size") or 0)
                if ws > 0:
                    wm_rect = (wx, wy, ws)
            except ValueError:
                wm_rect = None

            if media_path and (blur_media or add_watermark):
                if media_type == "תמונה":
                    final_media_path = process_image_blur_and_watermark(
                        media_path,
                        blur=blur_media,
                        add_wm=add_watermark,
                        blur_rect=blur_rect,
                        wm_rect=wm_rect,
                    )
                elif media_type == "וידאו":
                    final_media_path = process_video_blur_and_watermark(
                        media_path,
                        blur=blur_media,
                        add_wm=add_watermark,
                        blur_rect=blur_rect,
                        wm_rect=wm_rect,
                    )

            if send_to_telegram:
                if final_media_path and media_type in ("תמונה", "וידאו", "קובץ"):
                    await tg_client.send_file(
                        target,
                        final_media_path,
                        caption=text_to_send,
                    )
                else:
                    await tg_client.send_message(target, text_to_send)

            if send_to_facebook:
                # כאן תוסיף את הפונקציות שלך לפייסבוק אם תרצה
                pass

        asyncio.run_coroutine_threadsafe(do_send(), loop)
        flash("בקשת השליחה נשלחה ✔", "success")
        return redirect(url_for("messages_list"))

    # GET – תצוגת פרטי הודעה + קנבס
    previews = {}
    if msg.get("has_media"):
        mp = msg.get("media_path")
        if mp:
            rel = os.path.relpath(mp, DATA_DIR)
            previews["original"] = url_for("media", filename=rel)

    return render_template(
        "message_detail.html",
        message=msg,
        settings=settings,
        previews=previews,
    )


@app.route("/new", methods=["GET", "POST"])
def new_message():
    if request.method == "POST":
        text = request.form.get("text", "")
        translate_to_he = bool(request.form.get("translate_he"))
        send_to_telegram = bool(request.form.get("send_tg"))
        send_to_facebook = bool(request.form.get("send_fb"))

        if translate_to_he and text:
            try:
                text = GoogleTranslator(source="auto", target="iw").translate(text)
            except Exception as e:
                logging.error(f"Translation failed: {e}")
                flash(f"שגיאה בתרגום: {e}", "danger")

        try:
            future = asyncio.run_coroutine_threadsafe(ensure_tg_client(), loop)
            future.result(timeout=30)
        except Exception as e:
            logging.error(f"Telegram init error: {e}")
            flash(str(e), "danger")
            return redirect(url_for("new_message"))

        async def do_send_new():
            global tg_client
            target = settings.get("default_channel", "me")
            if send_to_telegram:
                await tg_client.send_message(target, text)
            if send_to_facebook:
                # כאן אפשר להוסיף אינטגרציה לפייסבוק
                pass

        asyncio.run_coroutine_threadsafe(do_send_new(), loop)

        now = datetime.utcnow().isoformat(timespec="seconds")
        new_id = (max((m["message_id"] for m in MESSAGES), default=0) + 1)
        MESSAGES.append({
            "message_id": new_id,
            "text": text,
            "sender": "אתה",
            "date": now,
            "timestamp": now,
            "has_media": False,
            "media_type": "",
            "media_path": "",
        })
        save_messages()
        trim_old_messages()

        flash("ההודעה נשלחה ונשמרה ✔", "success")
        return redirect(url_for("messages_list"))

    return render_template("new.html", settings=settings)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    global settings, login_client

    if request.method == "POST":
        api_id = request.form.get("api_id", "").strip()
        api_hash = request.form.get("api_hash", "").strip()
        phone = request.form.get("phone", "").strip()
        default_channel = request.form.get("default_channel", "me").strip()
        signature_text = request.form.get("signature_text", "")
        fb_token = request.form.get("facebook_page_access_token", "").strip()
        fb_page_id = request.form.get("facebook_page_id", "").strip()

        wm_file = request.files.get("watermark_image")
        if wm_file and wm_file.filename:
            try:
                im = Image.open(wm_file.stream).convert("RGBA")
                out_name = "watermark.png"
                out_path = os.path.join(APP_DATA_DIR, out_name)
                os.makedirs(APP_DATA_DIR, exist_ok=True)
                im.save(out_path, "PNG")
                settings["watermark_image"] = out_name
                logging.info(f"Watermark image saved to {out_path}")
            except Exception as e:
                logging.error(f"Failed to save watermark image: {e}")
                flash(f"שגיאה בשמירת סימן מים: {e}", "danger")

        settings["api_id"] = api_id
        settings["api_hash"] = api_hash
        settings["phone"] = phone
        settings["default_channel"] = default_channel
        settings["signature_text"] = signature_text
        settings["facebook_page_access_token"] = fb_token
        settings["facebook_page_id"] = fb_page_id

        save_settings(settings)
        flash("ההגדרות נשמרו ✔", "success")

        login_step = request.form.get("login_step")

        if login_step == "send_code":

            async def send_code():
                global login_client
                if not api_id or not api_hash or not phone:
                    raise RuntimeError("חייבים למלא API ID, API Hash ומספר טלפון")

                try:
                    api_id_int = int(api_id)
                except ValueError:
                    raise RuntimeError("API ID חייב להיות מספר")

                if login_client is not None:
                    try:
                        await login_client.disconnect()
                    except Exception:
                        pass
                    login_client = None

                client = TelegramClient(StringSession(""), api_id_int, api_hash, loop=loop)
                await client.connect()
                await client.send_code_request(phone)
                login_client = client

            try:
                future = asyncio.run_coroutine_threadsafe(send_code(), loop)
                future.result(timeout=30)
                flash("קוד נשלח לטלגרם ✔ – עכשיו הקלד את הקוד ולחץ 'אישור התחברות'", "success")
            except Exception as e:
                logging.error(f"Send code error: {e}", exc_info=True)
                flash(f"שגיאה בשליחת קוד: {e}", "danger")

        elif login_step == "confirm_code":
            code = request.form.get("code", "").strip()
            password = request.form.get("password", "").strip() or None

            async def do_login():
                global login_client, settings

                if login_client is None:
                    raise RuntimeError("לא נשלח קוד או שהחיבור פג – לחץ שוב 'שליחת קוד'")

                try:
                    await login_client.sign_in(
                        phone=phone or settings.get("phone", ""),
                        code=code,
                        password=password,
                    )
                except SessionPasswordNeededError:
                    raise RuntimeError("דרושה סיסמת 2FA – מלא ונסה שוב")
                except PhoneCodeInvalidError:
                    raise RuntimeError("קוד אימות שגוי – ודא שאתה מקליד את הקוד האחרון שהגיע")
                except PhoneCodeExpiredError:
                    try:
                        await login_client.disconnect()
                    except Exception:
                        pass
                    login_client = None
                    raise RuntimeError("קוד האימות פג תוקף – לחץ שוב 'שליחת קוד' והשתמש בקוד החדש שמגיע")

                session_str = login_client.session.save()
                settings["session"] = session_str
                save_settings(settings)

                try:
                    await login_client.disconnect()
                except Exception:
                    pass
                login_client = None

            try:
                future = asyncio.run_coroutine_threadsafe(do_login(), loop)
                future.result(timeout=30)
                flash("ההתחברות לטלגרם נשמרה ✔ – אין צורך להתחבר שוב", "success")
            except Exception as e:
                logging.error(f"Login error: {e}", exc_info=True)
                flash(str(e), "danger")

        return redirect(url_for("settings_page"))

    return render_template("settings.html", settings=settings)


# -------------------------------------------------------------------
# הפעלת האפליקציה
# -------------------------------------------------------------------

# -------------------------------------------------------------------
# אבטחה בסיסית – התחברות עם סיסמת מנהל
# -------------------------------------------------------------------

def is_logged_in() -> bool:
    return session.get("logged_in") is True


@app.before_request
def require_login():
    """
    לפני כל בקשה – לבדוק אם המשתמש מחובר.
    מותר גישה חופשית רק ל:
    - /login
    - /static (קבצי css/js)
    """
    # בקשות סטטיות /favicon וכו'
    if request.path.startswith("/static"):
        return

    # endpoint יכול להיות None לפעמים
    endpoint = request.endpoint or ""

    # מסך התחברות פתוח לכולם
    if endpoint == "login":
        return

    # אם לא מחובר – תמיד להפנות ל-login
    if not is_logged_in():
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    מסך התחברות:
    - אם admin_password עדיין ריק -> הסיסמה שתוזן בפעם הראשונה תהפוך לסיסמת המנהל.
    - אחרת צריך להזין את הסיסמה הקיימת.
    """
    global settings

    # אם כבר מחובר – לשלוח ישר לרשימת ההודעות
    if is_logged_in():
        return redirect(url_for("messages_list"))

    if request.method == "POST":
        password = (request.form.get("password") or "").strip()

        # פעם ראשונה – אין סיסמת מנהל עדיין
        if not settings.get("admin_password"):
            if not password:
                flash("צריך להגדיר סיסמה כלשהי כדי להמשיך", "danger")
                return redirect(url_for("login"))

            settings["admin_password"] = password
            save_settings(settings)
            session["logged_in"] = True
            flash("סיסמת מנהל הוגדרה והתחברת בהצלחה ✔", "success")
            return redirect(url_for("messages_list"))

        # יש כבר סיסמה – צריך להתאים
        if password == settings.get("admin_password"):
            session["logged_in"] = True
            flash("התחברת בהצלחה ✔", "success")
            return redirect(url_for("messages_list"))
        else:
            flash("סיסמה לא נכונה", "danger")
            return redirect(url_for("login"))

    # GET
    return render_template("login.html")


@app.route("/logout")
def logout():
    """
    יציאה – מחיקת session התחברות.
    """
    session.pop("logged_in", None)
    flash("התנתקת בהצלחה", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
