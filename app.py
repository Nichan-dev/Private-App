import eventlet
eventlet.monkey_patch()

import os
import random
import re
import string
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

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
    return render_template("index.html")


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
