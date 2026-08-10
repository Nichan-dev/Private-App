import eventlet
eventlet.monkey_patch()

import json
import os
import random
import re
import string
import uuid
from datetime import datetime

from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# room_code -> {"members": {sid: name}, "label": str}
rooms = {}

# รหัสส่วนตัวของแต่ละคนที่ออนไลน์อยู่ตอนนี้ (ไว้แอดเพื่อน)
ONLINE_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # ตัดตัวที่สับสนง่าย เช่น O/0, I/1
online_codes = {}   # code -> sid
sid_to_code = {}    # sid -> code

# ข้อความที่ส่งหาเพื่อนตอนเพื่อนออฟไลน์ -> ค้างไว้ตรงนี้ รอเพื่อนออนไลน์แล้วค่อยส่งให้
# (เก็บแค่ในหน่วยความจำ ถ้าเซิร์ฟเวอร์รีสตาร์ท/พักตัว ข้อความที่ค้างจะหายไป)
friend_messages_pending = {}  # code -> [{"from": code, "text": str, "time": str}, ...]

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

# ธีม UI (สี/ฟอนต์) ที่แอดมินปรับแต่งได้ ใช้ร่วมกันทั้งหน้าผู้ใช้และหน้าแอดมิน
# เก็บลงไฟล์ theme_settings.json ด้วย เพื่อให้ค่าที่ตั้งไว้อยู่ถาวรข้ามการรีสตาร์ทเซิร์ฟเวอร์
# (ต่างจาก state อื่นๆ ของแอปนี้ที่เก็บแค่ในหน่วยความจำ)
DEFAULT_THEME = {
    "primary": "#8b7cf9",
    "bg_top": "#1a1625",
    "card": "#201a2c",
    "text": "#f1eef8",
    "font": '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
}
THEME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme_settings.json")

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
FONT_PATTERN = re.compile(r"^[A-Za-z0-9 ,\-\"']{1,120}$")


def load_theme():
    try:
        with open(THEME_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_THEME)

    theme = dict(DEFAULT_THEME)
    for key in ("primary", "bg_top", "card", "text"):
        value = saved.get(key)
        if isinstance(value, str) and COLOR_PATTERN.match(value):
            theme[key] = value
    font = saved.get("font")
    if isinstance(font, str) and FONT_PATTERN.match(font):
        theme["font"] = font
    return theme


def save_theme():
    try:
        with open(THEME_FILE, "w", encoding="utf-8") as f:
            json.dump(theme_settings, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


theme_settings = load_theme()


def darken_hex(hex_color, factor=0.15):
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    r = max(0, round(r * (1 - factor)))
    g = max(0, round(g * (1 - factor)))
    b = max(0, round(b * (1 - factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def theme_context():
    ctx = dict(theme_settings)
    ctx["primary_dark"] = darken_hex(theme_settings["primary"])
    return ctx


# รายงานปัญหาที่ผู้ใช้ส่งมา -> เก็บลงไฟล์ด้วยเหมือนธีม เพื่อไม่ให้หายตอนเซิร์ฟเวอร์รีสตาร์ท
# (แต่จะหายถ้ามีการ deploy โค้ดใหม่ เพราะ Render สร้าง container ใหม่ทุกครั้งที่ deploy)
REPORTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports.json")


def load_reports():
    try:
        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(saved, list):
        return []
    return [
        r for r in saved
        if isinstance(r, dict) and isinstance(r.get("id"), str)
        and isinstance(r.get("message"), str) and isinstance(r.get("time"), str)
    ]


def save_reports():
    try:
        with open(REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


reports = load_reports()


def gen_room_code():
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if code not in rooms:
            return code


def gen_online_code():
    while True:
        code = "".join(random.choices(ONLINE_CODE_ALPHABET, k=6))
        if code not in online_codes:
            return code


@app.route("/")
def index():
    return render_template("index.html", theme=theme_context())


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "รหัสผ่านไม่ถูกต้อง"
    return render_template("admin_login.html", error=error, theme=theme_context())


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    room_list = [
        {
            "code": code,
            "label": info["label"],
            "members": list(info["members"].values()),
        }
        for code, info in rooms.items()
    ]
    pending_list = [
        {"code": code, "count": len(msgs)}
        for code, msgs in friend_messages_pending.items()
    ]
    return render_template(
        "admin.html",
        online_count=len(online_codes),
        room_list=room_list,
        pending_list=pending_list,
        reports=list(reversed(reports)),
        theme=theme_context(),
    )


@app.route("/admin/reports/delete", methods=["POST"])
@admin_required
def admin_delete_report():
    report_id = request.form.get("id")
    global reports
    reports = [r for r in reports if r["id"] != report_id]
    save_reports()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/theme", methods=["GET", "POST"])
@admin_required
def admin_theme():
    error = None
    if request.method == "POST":
        if request.form.get("reset"):
            theme_settings.update(DEFAULT_THEME)
            save_theme()
            return redirect(url_for("admin_theme"))

        new_theme = {
            "primary": request.form.get("primary", "").strip(),
            "bg_top": request.form.get("bg_top", "").strip(),
            "card": request.form.get("card", "").strip(),
            "text": request.form.get("text", "").strip(),
            "font": request.form.get("font", "").strip(),
        }
        if not all(COLOR_PATTERN.match(new_theme[k]) for k in ("primary", "bg_top", "card", "text")):
            error = "รหัสสีต้องอยู่ในรูปแบบ #RRGGBB เท่านั้น"
        elif not FONT_PATTERN.match(new_theme["font"]):
            error = "ชื่อฟอนต์มีอักขระที่ไม่อนุญาต (ใช้ได้แค่ตัวอักษร ตัวเลข เว้นวรรค , - \" ')"
        else:
            theme_settings.update(new_theme)
            save_theme()
            return redirect(url_for("admin_theme"))

    return render_template("admin_theme.html", theme=theme_context(), error=error)


@app.route("/api/create-room", methods=["POST"])
def create_room():
    data = request.get_json(silent=True) or {}
    custom_code = (data.get("code") or "").strip()
    label = (data.get("label") or "").strip()[:40]

    if custom_code:
        if not CODE_PATTERN.match(custom_code):
            return jsonify({"error": "รหัสห้องต้องเป็นตัวอักษร/ตัวเลข 3-20 ตัว (a-z, 0-9, -, _)"}), 400
        if custom_code in rooms:
            return jsonify({"error": "รหัสห้องนี้มีคนใช้อยู่แล้ว ลองรหัสอื่น"}), 409
        code = custom_code
    else:
        code = gen_room_code()

    rooms[code] = {"members": {}, "label": label}
    return jsonify({"room": code, "label": label})


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()[:500]
    code = (data.get("code") or "").strip().upper()[:10]

    if not message:
        return jsonify({"error": "กรุณาใส่ข้อความ"}), 400

    reports.append({
        "id": uuid.uuid4().hex[:8],
        "code": code or "ไม่ทราบ",
        "message": message,
        "time": datetime.now().strftime("%d/%m %H:%M"),
    })
    save_reports()
    return jsonify({"ok": True})


ONLINE_CODE_PATTERN = re.compile(r"^[A-Z2-9]{6}$")


@socketio.on("register_code")
def on_register_code(data):
    requested_code = (data.get("code") or "").strip().upper()

    if requested_code and ONLINE_CODE_PATTERN.match(requested_code):
        # เคยมีรหัสอยู่แล้ว (เก็บไว้ใน localStorage ฝั่งเบราว์เซอร์) -> ใช้รหัสเดิมต่อ
        # ถ้ามีการเชื่อมต่อเก่าค้างอยู่ (เช่น รีเฟรชหน้า) ให้ทับด้วยการเชื่อมต่อล่าสุด
        old_sid = online_codes.get(requested_code)
        if old_sid and old_sid != request.sid:
            sid_to_code.pop(old_sid, None)
        code = requested_code
    else:
        code = gen_online_code()

    online_codes[code] = request.sid
    sid_to_code[request.sid] = code
    emit("your_code", {"code": code})

    # มีข้อความที่เพื่อนส่งมาตอนที่เรายังไม่ออนไลน์ค้างอยู่ไหม -> ส่งให้ตอนนี้เลย
    pending = friend_messages_pending.pop(code, [])
    for msg in pending:
        emit("friend_message", msg)


@socketio.on("friend_request")
def on_friend_request(data):
    friend_code = (data.get("code") or "").strip().upper()
    my_code = sid_to_code.get(request.sid)
    if not friend_code or not my_code:
        return

    if friend_code == my_code:
        emit("friend_error", {"message": "นี่รหัสของตัวเองนะ ใส่รหัสของเพื่อนสิ"})
        return

    target_sid = online_codes.get(friend_code)
    if not target_sid:
        emit("friend_error", {"message": "ไม่พบเพื่อนคนนี้ หรือเขาไม่ได้ออนไลน์ตอนนี้ (ต้องออนไลน์พร้อมกันตอนขอเพิ่มเพื่อน)"})
        return

    emit("friend_request_received", {"code": my_code}, room=target_sid)


@socketio.on("friend_response")
def on_friend_response(data):
    friend_code = (data.get("code") or "").strip().upper()
    accepted = bool(data.get("accepted"))
    my_code = sid_to_code.get(request.sid)
    if not friend_code or not my_code:
        return

    target_sid = online_codes.get(friend_code)
    if target_sid:
        emit("friend_response_received", {"code": my_code, "accepted": accepted}, room=target_sid)


@socketio.on("friend_message")
def on_friend_message(data):
    friend_code = (data.get("code") or "").strip().upper()
    text = (data.get("text") or "").strip()
    my_code = sid_to_code.get(request.sid)
    if not friend_code or not my_code or not text:
        return

    msg = {"from": my_code, "text": text, "time": now_str()}
    target_sid = online_codes.get(friend_code)
    if target_sid:
        emit("friend_message", msg, room=target_sid)
    else:
        friend_messages_pending.setdefault(friend_code, []).append(msg)


@socketio.on("join")
def on_join(data):
    room = (data.get("room") or "").strip()
    name = (data.get("name") or "ไม่ระบุชื่อ").strip()[:30]

    if room not in rooms:
        emit("join_error", {"message": "ไม่พบห้องนี้ ตรวจสอบรหัสห้องอีกครั้ง"})
        return

    if len(rooms[room]["members"]) >= 2:
        emit("join_error", {"message": "ห้องนี้เต็มแล้ว (มีคนคุยอยู่ 2 คนแล้ว)"})
        return

    join_room(room)
    rooms[room]["members"][request.sid] = name

    emit("joined", {"room": room, "name": name, "label": rooms[room]["label"]})
    emit(
        "system",
        {"text": f"{name} เข้าห้องแล้ว", "time": now_str()},
        room=room,
    )


@socketio.on("message")
def on_message(data):
    room = data.get("room")
    text = (data.get("text") or "").strip()
    if not room or room not in rooms or request.sid not in rooms[room]["members"]:
        return
    if not text:
        return
    name = rooms[room]["members"][request.sid]
    emit(
        "message",
        {"name": name, "text": text, "time": now_str()},
        room=room,
    )


@socketio.on("typing")
def on_typing(data):
    room = data.get("room")
    if not room or room not in rooms or request.sid not in rooms[room]["members"]:
        return
    name = rooms[room]["members"][request.sid]
    emit("typing", {"name": name, "typing": bool(data.get("typing"))}, room=room, include_self=False)


@socketio.on("seen")
def on_seen(data):
    room = data.get("room")
    if not room or room not in rooms or request.sid not in rooms[room]["members"]:
        return
    reader_name = rooms[room]["members"][request.sid]
    emit("seen", {"name": reader_name}, room=room, include_self=False)


@socketio.on("friend_typing")
def on_friend_typing(data):
    friend_code = (data.get("code") or "").strip().upper()
    my_code = sid_to_code.get(request.sid)
    if not friend_code or not my_code:
        return
    target_sid = online_codes.get(friend_code)
    if target_sid:
        emit("friend_typing", {"code": my_code, "typing": bool(data.get("typing"))}, room=target_sid)


@socketio.on("friend_seen")
def on_friend_seen(data):
    friend_code = (data.get("code") or "").strip().upper()
    my_code = sid_to_code.get(request.sid)
    if not friend_code or not my_code:
        return
    target_sid = online_codes.get(friend_code)
    if target_sid:
        emit("friend_seen", {"code": my_code}, room=target_sid)


@socketio.on("disconnect")
def on_disconnect():
    code = sid_to_code.pop(request.sid, None)
    if code:
        online_codes.pop(code, None)

    for room, info in list(rooms.items()):
        if request.sid in info["members"]:
            name = info["members"].pop(request.sid)
            emit("system", {"text": f"{name} ออกจากห้องแล้ว", "time": now_str()}, room=room)
            leave_room(room)
            if not info["members"]:
                rooms.pop(room, None)


def now_str():
    return datetime.now().strftime("%H:%M")


if __name__ == "__main__":
    debug = os.environ.get("CHAT_DEBUG") == "1"
    port = int(os.environ.get("PORT", 5050))
    socketio.run(app, host="0.0.0.0", port=port, debug=debug)
