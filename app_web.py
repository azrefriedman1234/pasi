import os
import json
import logging
import asyncio
import mimetypes
import subprocess
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    request,
    render_template,
    redirect,
    url_for,
    flash,
    send_from_directory,
    session,
    current_app,
    abort,
)
from werkzeug.utils import secure_filename

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
)


# === Paths & constants ===

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
OUTPUT_DIR = DATA_DIR / "output"

for d in (DATA_DIR, MEDIA_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = DATA_DIR / "settings.json"
MESSAGES_PATH = DATA_DIR / "messages.json"
TELEGRAM_SESSION_PATH = DATA_DIR / "telegram.session"
WATERMARK_PATH = DATA_DIR / "watermark.png"

MAX_MESSAGES = 120
APP_PASSWORD = "7447"  # סיסמת כניסה לאפליקציה

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# === Flask app ===

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-pasiflonet-web")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 200  # עד 200MB לקבצי מדיה


# === Helpers: settings & messages ===

def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    # ברירות מחדל לשדות
    defaults = {
        "telegram_api_id": "",
        "telegram_api_hash": "",
        "telegram_phone": "",
        "telegram_password": "",
        "telegram_target": "",
        "telegram_sources": "",
        "facebook_page_token": "",
        "facebook_page_id": "",
    }
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def save_settings(settings: dict) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def load_messages() -> list[dict]:
    if MESSAGES_PATH.exists():
        try:
            with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            if isinstance(msgs, list):
                return msgs
        except Exception:
            pass
    return []


def save_messages(messages: list[dict]) -> None:
    with open(MESSAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def cleanup_old_messages() -> None:
    """
    מוחק הודעות ישנות (ומדיה שלהן) אם יש יותר מ־MAX_MESSAGES.
    """
    messages = load_messages()
    if len(messages) <= MAX_MESSAGES:
        return

    # למיין לפי created_at (ישן → חדש)
    def key_fn(m):
        return m.get("created_at") or ""

    messages_sorted = sorted(messages, key=key_fn)
    to_delete = len(messages_sorted) - MAX_MESSAGES
    if to_delete <= 0:
        return

    old = messages_sorted[:to_delete]
    keep = messages_sorted[to_delete:]

    # מחיקת קבצים ישנים
    for msg in old:
        for key, folder in (
            ("filename", MEDIA_DIR),
            ("thumb", MEDIA_DIR),
            ("processed_filename", OUTPUT_DIR),
        ):
            name = msg.get(key)
            if not name:
                continue
            p = folder / name
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                logging.exception("Failed deleting media file %s", p)

    save_messages(keep)


# === Telegram async helpers ===

async def _send_telegram_code_async(api_id: int, api_hash: str, phone: str) -> None:
    """
    שולח קוד אימות לטלגרם ושומר session בקובץ.
    """
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()
    await client.send_code_request(phone)
    await client.disconnect()


async def _login_telegram_async(
    api_id: int,
    api_hash: str,
    phone: str,
    code: str,
    password: str | None,
) -> None:
    """
    מבצע login עם קוד האימות ושומר session קבוע.
    """
    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()
    await client.sign_in(phone=phone, code=code, password=password or None)
    await client.disconnect()


async def _send_telegram_message_async(settings: dict, msg: dict) -> None:
    """
    שולח הודעה/מדיה לטלגרם לפי ההגדרות וההודעה.
    """
    api_id_raw = settings.get("telegram_api_id") or ""
    api_hash = settings.get("telegram_api_hash") or ""
    target = settings.get("telegram_target") or ""

    if not api_id_raw or not api_hash or not target:
        raise RuntimeError("חסרות הגדרות טלגרם (API ID / API Hash / יעד)")

    try:
        api_id = int(api_id_raw)
    except ValueError:
        raise RuntimeError("Telegram API ID לא מספרי")

    client = TelegramClient(str(TELEGRAM_SESSION_PATH), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("המשתמש לא מחובר – התחבר לטלגרם דרך ההגדרות")

    text = (msg.get("text") or "").strip()

    media_path = None
    if msg.get("processed_filename"):
        media_path = OUTPUT_DIR / msg["processed_filename"]
    elif msg.get("filename"):
        media_path = MEDIA_DIR / msg["filename"]

    if media_path and media_path.exists():
        await client.send_file(target, str(media_path), caption=text or None)
    else:
        if not text:
            text = " "
        await client.send_message(target, text)

    await client.disconnect()


# === Media processing ===

from PIL import Image, ImageFilter  # noqa: E402


def process_image(msg: dict, use_blur: bool, use_watermark: bool,
                  x: int, y: int, w: int, h: int) -> None:
    if not msg.get("filename"):
        return

    src_path = MEDIA_DIR / msg["filename"]
    if not src_path.exists():
        return

    img = Image.open(src_path).convert("RGBA")

    if use_blur:
        # להבטיח גבולות חוקיים
        x_clamp = max(0, min(x, img.width - 1))
        y_clamp = max(0, min(y, img.height - 1))
        w_clamp = max(10, min(w, img.width - x_clamp))
        h_clamp = max(10, min(h, img.height - y_clamp))

        box = (x_clamp, y_clamp, x_clamp + w_clamp, y_clamp + h_clamp)
        region = img.crop(box)
        blurred = region.filter(ImageFilter.GaussianBlur(radius=16))
        img.paste(blurred, box)

    if use_watermark and WATERMARK_PATH.exists():
        wm = Image.open(WATERMARK_PATH).convert("RGBA")
        scale = min(img.width * 0.25 / wm.width, img.height * 0.25 / wm.height, 1.0)
        wm_size = (int(wm.width * scale), int(wm.height * scale))
        wm = wm.resize(wm_size, Image.LANCZOS)

        margin = 16
        pos = (img.width - wm.width - margin, img.height - wm.height - margin)

        base = Image.new("RGBA", img.size)
        base = Image.alpha_composite(base, img)
        base = Image.alpha_composite(base, wm=wm if False else Image.new("RGBA", (0, 0)))
        # קצת טריק: נעשה overlay ידני
        img.alpha_composite(wm, dest=pos)

    out_name = f"{src_path.stem}_processed.png"
    out_path = OUTPUT_DIR / out_name
    img.save(out_path, "PNG")

    msg["processed_filename"] = out_name


def process_video(msg: dict, use_blur: bool, use_watermark: bool,
                  x: int, y: int, w: int, h: int) -> None:
    if not msg.get("filename"):
        return

    src_path = MEDIA_DIR / msg["filename"]
    if not src_path.exists():
        return

    out_name = f"{src_path.stem}_processed.mp4"
    out_path = OUTPUT_DIR / out_name

    # אם אין טשטוש ואין סימן מים – נעתיק כמו שהוא
    if not use_blur and not use_watermark:
        import shutil
        shutil.copyfile(src_path, out_path)
        msg["processed_filename"] = out_name
        return

    filters = ""

    if use_blur:
        # טשטוש אזורי: split + crop + boxblur + overlay
        filters = (
            f"[0:v]split=2[main][tmp];"
            f"[tmp]crop={w}:{h}:{x}:{y},boxblur=15[blurred];"
            f"[main][blurred]overlay={x}:{y}:format=auto[with_blur]"
        )

    if use_watermark and WATERMARK_PATH.exists():
        if use_blur:
            filters = (
                filters +
                ";[with_blur][1:v]overlay=W-w-20:H-h-20:format=auto[out]"
            )
        else:
            filters = (
                "[0:v][1:v]overlay=W-w-20:H-h-20:format=auto[out]"
            )
    else:
        if use_blur:
            filters = filters + "[out]"

    cmd = ["ffmpeg", "-y", "-i", str(src_path)]

    if use_watermark and WATERMARK_PATH.exists():
        cmd += ["-i", str(WATERMARK_PATH)]

    if filters:
        cmd += [
            "-filter_complex", filters,
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "copy",
        ]
    else:
        # fallback
        cmd += [
            "-c:v", "copy",
            "-c:a", "copy",
        ]

    cmd.append(str(out_path))

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        msg["processed_filename"] = out_name
    except Exception:
        logging.exception("ffmpeg video processing failed")
        # אם נכשל – לפחות ננסה להעתיק את המקור
        import shutil
        shutil.copyfile(src_path, out_path)
        msg["processed_filename"] = out_name


def generate_video_thumb(src_filename: str) -> str | None:
    src_path = MEDIA_DIR / src_filename
    if not src_path.exists():
        return None

    thumb_name = f"{src_path.stem}_thumb.jpg"
    thumb_path = MEDIA_DIR / thumb_name

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src_path),
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(thumb_path),
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumb_name
    except Exception:
        logging.exception("ffmpeg thumb failed")
        return None


# === Auth guard (כניסה בסיסמה לאתר) ===

@app.before_request
def require_app_login():
    # לא להפריע לסטטיק ולדף הלוגין עצמו
    if request.endpoint in ("login", "static"):
        return
    if request.path.startswith("/static/"):
        return

    if not session.get("app_authed"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == APP_PASSWORD:
            session["app_authed"] = True
            flash("התחברת בהצלחה", "success")
            return redirect(url_for("messages"))
        else:
            flash("סיסמה שגויה", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("app_authed", None)
    flash("נותקת מהמערכת", "success")
    return redirect(url_for("login"))


# === Routes ===

@app.route("/")
def index():
    return redirect(url_for("messages"))


@app.route("/messages")
def messages():
    msgs = load_messages()
    msgs_sorted = sorted(msgs, key=lambda m: m.get("created_at") or "", reverse=True)
    return render_template("messages.html", messages=msgs_sorted)


@app.route("/new", methods=["GET", "POST"])
def new_message():
    if request.method == "POST":
        text = (request.form.get("text") or "").strip()
        send_telegram = bool(request.form.get("send_telegram"))

        file = request.files.get("media")
        media_type = ""
        filename = ""
        thumb = None

        if file and file.filename:
            filename = secure_filename(file.filename)
            # למנוע התנגשויות
            base, ext = os.path.splitext(filename)
            ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
            filename = f"{base}_{ts}{ext}"
            dest = MEDIA_DIR / filename
            file.save(dest)

            mime, _ = mimetypes.guess_type(filename)
            if mime and mime.startswith("image"):
                media_type = "image"
            elif mime and mime.startswith("video"):
                media_type = "video"
                thumb = generate_video_thumb(filename)
            else:
                media_type = "file"

        messages_list = load_messages()
        next_id = max((m.get("id", 0) for m in messages_list), default=0) + 1

        msg = {
            "id": next_id,
            "text": text,
            "media_type": media_type,
            "filename": filename,
            "thumb": thumb,
            "processed_filename": "",
            "created_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        messages_list.append(msg)
        save_messages(messages_list)

        # ניקוי ישנים
        cleanup_old_messages()

        # שליחה לטלגרם אם סומן
        if send_telegram:
            settings = load_settings()
            try:
                asyncio.run(_send_telegram_message_async(settings, msg))
                flash("ההודעה נשלחה לטלגרם ✔", "success")
            except Exception as e:
                current_app.logger.exception("Telegram send error")
                flash(f"שגיאה בשליחה לטלגרם: {e}", "danger")

        flash("ההודעה נשמרה", "success")
        return redirect(url_for("messages"))

    return render_template("new.html")


@app.route("/edit/<int:msg_id>")
def edit(msg_id: int):
    messages_list = load_messages()
    msg = next((m for m in messages_list if m.get("id") == msg_id), None)
    if not msg:
        abort(404)
    return render_template("edit.html", msg=msg)


@app.route("/process/<int:msg_id>", methods=["POST"])
def process(msg_id: int):
    messages_list = load_messages()
    msg = next((m for m in messages_list if m.get("id") == msg_id), None)
    if not msg:
        abort(404)

    use_blur = bool(request.form.get("use_blur"))
    use_watermark = bool(request.form.get("use_watermark"))

    def parse_int(name: str, default: int) -> int:
        try:
            return int(request.form.get(name, default))
        except Exception:
            return default

    x = parse_int("region_x", 50)
    y = parse_int("region_y", 50)
    w = parse_int("region_w", 200)
    h = parse_int("region_h", 120)

    if msg.get("media_type") == "image":
        process_image(msg, use_blur, use_watermark, x, y, w, h)
    elif msg.get("media_type") == "video":
        process_video(msg, use_blur, use_watermark, x, y, w, h)
    else:
        flash("אין מדיה לעיבוד בהודעה הזו", "danger")
        return redirect(url_for("edit", msg_id=msg_id))

    # לשמור את ההודעה המעודכנת
    for i, m in enumerate(messages_list):
        if m.get("id") == msg_id:
            messages_list[i] = msg
            break
    save_messages(messages_list)

    flash("העיבוד הושלם ✔", "success")
    return redirect(url_for("edit", msg_id=msg_id))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings = load_settings()
    watermark_exists = WATERMARK_PATH.exists()

    if request.method == "POST":
        action = request.form.get("action", "save")

        # עדכון הגדרות מטופס
        settings["telegram_api_id"] = (request.form.get("telegram_api_id") or "").strip()
        settings["telegram_api_hash"] = (request.form.get("telegram_api_hash") or "").strip()
        settings["telegram_phone"] = (request.form.get("telegram_phone") or "").strip()
        settings["telegram_password"] = (request.form.get("telegram_password") or "").strip()
        settings["telegram_target"] = (request.form.get("telegram_target") or "").strip()
        settings["telegram_sources"] = (request.form.get("telegram_sources") or "").strip()

        settings["facebook_page_token"] = (request.form.get("facebook_page_token") or "").strip()
        settings["facebook_page_id"] = (request.form.get("facebook_page_id") or "").strip()

        # העלאת סימן מים אם יש
        wm_file = request.files.get("watermark_image")
        if wm_file and wm_file.filename:
            wm_path = WATERMARK_PATH
            wm_file.save(wm_path)
            current_app.logger.info("Watermark image saved to %s", wm_path)
            watermark_exists = True

        # נשמור תמיד את ההגדרות
        save_settings(settings)

        api_id_raw = settings.get("telegram_api_id") or ""
        api_hash = settings.get("telegram_api_hash") or ""
        phone = settings.get("telegram_phone") or ""
        password = settings.get("telegram_password") or ""

        try:
            api_id = int(api_id_raw) if api_id_raw else None
        except ValueError:
            api_id = None

        if action == "send_code":
            if not api_id or not api_hash or not phone:
                flash("חסרים API ID / API Hash / טלפון", "danger")
                return redirect(url_for("settings_page"))
            try:
                asyncio.run(_send_telegram_code_async(api_id, api_hash, phone))
                flash("קוד נשלח לטלגרם ✔", "success")
            except Exception as e:
                current_app.logger.exception("Send code error")
                flash(f"שגיאה בשליחת קוד: {e}", "danger")
            return redirect(url_for("settings_page"))

        if action == "login":
            code = (request.form.get("login_code") or "").strip()

            if not api_id or not api_hash or not phone:
                flash("חסרים API ID / API Hash / טלפון", "danger")
                return redirect(url_for("settings_page"))

            if not code:
                flash("צריך להזין קוד אימות", "danger")
                return redirect(url_for("settings_page"))

            try:
                asyncio.run(_login_telegram_async(api_id, api_hash, phone, code, password))
                flash("התחברות לטלגרם הצליחה ✔", "success")
            except PhoneCodeExpiredError:
                flash("קוד האימות פג תוקף – בקש קוד חדש", "danger")
            except PhoneCodeInvalidError:
                flash("קוד האימות שגוי – נסה שוב", "danger")
            except Exception as e:
                current_app.logger.exception("Login error")
                flash(f"שגיאה בהתחברות: {e}", "danger")

            return redirect(url_for("settings_page"))

        flash("ההגדרות נשמרו ✔", "success")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html",
        settings=settings,
        watermark_exists=watermark_exists,
    )


# === Static media routes ===

@app.route("/media/<path:filename>")
def media_file(filename: str):
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/output/<path:filename>")
def output_file(filename: str):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    # להרצה מקומית
    app.run(host="0.0.0.0", port=5000, debug=True)
