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

CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,20}$")


def gen_room_code():
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if code not in rooms:
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
