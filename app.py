import os, json, copy, re
from datetime import datetime
from flask import Flask, render_template_string, request, Response
from flask_socketio import SocketIO, emit
import openai
import google.generativeai as genai

# =========================
# Env
# =========================
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "save_data.json")

ADMIN_PASSWORD_ENV = os.getenv("ADMIN_PASSWORD", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# =========================
# AI clients
# =========================
client = openai.OpenAI(api_key=OPENAI_API_KEY)

gemini_model = None
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-3-pro-preview')

# =========================
# Flask / Socket
# =========================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*")

# =========================
# Storage
# =========================
def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return None
    return None

# =========================
# State
# =========================
initial_state = {
    "session_title": "드림놀이",
    "theme": {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"},
    "ai_model": "gpt-5.2",
    "admin_password": "3896",

    "solo_mode": False,
    "session_started": False,

    "profiles": {
        "user1": {"name": "Player 1", "bio": "", "canon": "", "locked": False},
        "user2": {"name": "Player 2", "bio": "", "canon": "", "locked": False}
    },

    "pending_inputs": {},

    "ai_history": [],
    "summary": "",
    "prologue": "",
    "sys_prompt": "당신은 숙련된 TRPG 마스터입니다.",

    "lorebook": [],
    "examples": [{"q": "", "a": ""}, {"q": "", "a": ""}, {"q": "", "a": ""}]
}

saved_state = load_data()
state = saved_state if isinstance(saved_state, dict) else copy.deepcopy(initial_state)

if ADMIN_PASSWORD_ENV:
    state["admin_password"] = ADMIN_PASSWORD_ENV

# 접속/관리자
connected_users = {"user1": None, "user2": None}
readonly_sids = set()
admin_sids = set()

# 입력중 표시
typing_users = set()

# =========================
# Helpers
# =========================
def sanitize_filename(name: str) -> str:
    name = (name or "session").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    return name[:60] or "session"

def get_export_config_only():
    return {
        "session_title": state.get("session_title", ""),
        "sys_prompt": state.get("sys_prompt", ""),
        "prologue": state.get("prologue", ""),
        "ai_model": state.get("ai_model", "gpt-5.2"),
        "examples": state.get("examples", [{"q":"","a":""},{"q":"","a":""},{"q":"","a":""}]),
        "lorebook": state.get("lorebook", []),
        "solo_mode": bool(state.get("solo_mode", False)),
        "_export_type": "dream_config_only_v1"
    }

def import_config_only(data: dict):
    allow = {"session_title","sys_prompt","prologue","ai_model","examples","lorebook","solo_mode"}
    for k in allow:
        if k in data:
            state[k] = copy.deepcopy(data[k])

def get_sanitized_state():
    safe = copy.deepcopy(state)
    safe["profiles"]["user1"]["bio"] = ""
    safe["profiles"]["user1"]["canon"] = ""
    safe["profiles"]["user2"]["bio"] = ""
    safe["profiles"]["user2"]["canon"] = ""
    return safe

def emit_state_to_players():
    save_data()
    payload = get_sanitized_state()
    payload["pending_status"] = list(state.get("pending_inputs", {}).keys())
    payload["typing_status"] = list(typing_users)

    if connected_users["user1"]:
        socketio.emit("initial_state", payload, room=connected_users["user1"])
    if connected_users["user2"]:
        socketio.emit("initial_state", payload, room=connected_users["user2"])

def analyze_theme_color(title, sys_prompt):
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"웹 UI 컬러 팔레트 전문가입니다. 반드시 JSON으로만 답변하세요: {\"bg\":\"#RRGGBB\",\"panel\":\"#RRGGBB\",\"accent\":\"#RRGGBB\"}"},
                {"role":"user","content":f"세션 제목: {title}\n시스템 프롬프트 요약: {sys_prompt[:800]}"}
            ],
            response_format={"type":"json_object"}
        )
        obj = json.loads(res.choices[0].message.content)
        out = state.get("theme", {"bg":"#ffffff","panel":"#f1f3f5","accent":"#e91e63"})
        for k in ("bg","panel","accent"):
            if isinstance(obj.get(k), str) and obj[k].startswith("#"):
                out[k] = obj[k]
        return out
    except:
        return state.get("theme", {"bg":"#ffffff","panel":"#f1f3f5","accent":"#e91e63"})

# -------------------------
# Context / Summary
# -------------------------
MAX_CONTEXT_CHARS_BUDGET = 14000
HISTORY_SOFT_LIMIT_CHARS = 9500
SUMMARY_MAX_CHARS = 500
TARGET_MAX_TOKENS = 1100

def build_history_block():
    history = state.get("ai_history", [])
    collected, total = [], 0
    for msg in reversed(history):
        add_len = len(msg) + 1
        if total + add_len > HISTORY_SOFT_LIMIT_CHARS:
            break
        collected.append(msg)
        total += add_len
    collected.reverse()
    return collected

def would_overflow_context(extra_incoming: str) -> bool:
    sys_p = state.get("sys_prompt","")
    pro = state.get("prologue","")
    summ = state.get("summary","")
    hist = "\n".join(build_history_block())
    rough = len(sys_p) + len(pro) + len(summ) + len(hist) + len(extra_incoming) + 2000
    return rough > MAX_CONTEXT_CHARS_BUDGET

def auto_summary_apply():
    def run_once():
        recent_log = "\n".join(state.get("ai_history", [])[-60:])
        if not recent_log:
            return None
        prompt = (
            "당신은 TRPG 진행 보조 AI입니다.\n"
            "아래 최근 대화를 바탕으로 '현재 상황 요약'을 2~3문장으로 작성해 주세요.\n"
            "사실/행동/목표 중심으로 간결하게 작성해 주세요.\n\n"
            f"[최근 대화]\n{recent_log}"
        )
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}]
        )
        return (res.choices[0].message.content or "").strip()

    try:
        s = run_once() or run_once()
        if s:
            state["summary"] = s[:SUMMARY_MAX_CHARS]
            save_data()
    except:
        pass

# =========================
# Routes
# =========================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, theme=state.get("theme"))

@app.route("/export")
def export_config():
    cfg = get_export_config_only()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"{sanitize_filename(cfg.get('session_title'))}_{ts}.json"
    return Response(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={fname}"}
    )

@app.route("/import", methods=["POST"])
def import_config():
    try:
        if "file" not in request.files:
            return "파일이 없습니다.", 400
        file = request.files["file"]
        if file.filename == "":
            return "선택된 파일이 없습니다.", 400
        data = json.load(file)
        if not isinstance(data, dict):
            return "올바른 JSON이 아닙니다.", 400

        import_config_only(data)
        save_data()
        emit_state_to_players()
        socketio.emit("reload_signal")
        return "OK", 200
    except Exception as e:
        return str(e), 500

# =========================
# Socket: join / disconnect
# =========================
@socketio.on("join_game")
def join_game(data=None):
    sid = request.sid
    saved_role = data.get("saved_role") if data else None

    # 1. 새로고침한 유저가 기존 역할을 가지고 있는 경우 재연결
    if saved_role in connected_users and connected_users[saved_role] is None:
        connected_users[saved_role] = sid
        emit("assign_role", {"role": saved_role, "mode": "player"})
        emit_state_to_players()
        return

    # 2. 기존 로직 (신규 접속)
    for role, rsid in connected_users.items():
        if rsid == sid:
            emit("assign_role", {"role": role, "mode": "player"})
            emit_state_to_players()
            return

    if connected_users["user1"] is None:
        connected_users["user1"] = sid
        emit("assign_role", {"role": "user1", "mode": "player"})
        emit_state_to_players()
        return

    if connected_users["user2"] is None:
        connected_users["user2"] = sid
        emit("assign_role", {"role": "user2", "mode": "player"})
        emit_state_to_players()
        return

    # 그 외는 읽기 전용
    readonly_sids.add(sid)
    emit("assign_role", {"role": "readonly", "mode": "readonly"})
    emit("initial_state", get_sanitized_state())
    
@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    admin_sids.discard(sid)

    for role in ("user1","user2"):
        if connected_users[role] == sid:
            connected_users[role] = None
            typing_users.discard(role)
            state.get("pending_inputs", {}).pop(role, None)

    readonly_sids.discard(sid)
    save_data()
    emit_state_to_players()

# =========================
# Socket: typing indicator
# =========================
@socketio.on("start_typing")
def start_typing(data):
    uid = data.get("uid")
    if uid in ("user1","user2") and connected_users.get(uid) == request.sid:
        typing_users.add(uid)
        emit_state_to_players()

@socketio.on("stop_typing")
def stop_typing(data):
    uid = data.get("uid")
    if uid in ("user1","user2"):
        typing_users.discard(uid)
        emit_state_to_players()

# =========================
# Socket: admin / save
# =========================
@socketio.on("check_admin")
def check_admin(data):
    ok = str(data.get("password")) == str(state.get("admin_password"))
    if ok:
        admin_sids.add(request.sid)
    emit("admin_auth_res", {"success": ok})

@socketio.on("save_master_base")
def save_master_base(data):
    state["session_title"] = (data.get("title", state["session_title"]) or "")[:30]
    state["sys_prompt"] = (data.get("sys", state["sys_prompt"]) or "")[:4000]
    state["prologue"] = (data.get("pro", state["prologue"]) or "")[:1000]
    state["summary"] = (data.get("sum", state["summary"]) or "")[:SUMMARY_MAX_CHARS]
    state["ai_model"] = data.get("model", state.get("ai_model","gpt-5.2"))
    state["solo_mode"] = bool(data.get("solo_mode", state.get("solo_mode", False)))

    save_data()
    emit_state_to_players()

@socketio.on("theme_analyze_request")
def theme_analyze_request(_=None):
    if not (state.get("sys_prompt","").strip() and state.get("prologue","").strip()):
        return
    state["theme"] = analyze_theme_color(state.get("session_title",""), state.get("sys_prompt",""))
    save_data()
    emit_state_to_players()
    socketio.emit("reload_signal")

@socketio.on("save_examples")
def save_examples(data):
    out = []
    for i in range(3):
        ex = data[i] if i < len(data) else {"q":"","a":""}
        out.append({"q": (ex.get("q","") or "")[:500], "a": (ex.get("a","") or "")[:500]})
    state["examples"] = out
    save_data()
    emit_state_to_players()

@socketio.on("update_profile")
def update_profile(data):
    uid = data.get("uid")
    if uid not in ("user1","user2"):
        return
    if connected_users.get(uid) != request.sid:
        return
    if state["profiles"][uid].get("locked"):
        return

    name = (data.get("name") or "").strip()
    if not name:
        return

    state["profiles"][uid]["name"] = name[:12]
    state["profiles"][uid]["bio"] = (data.get("bio") or "")[:200]
    state["profiles"][uid]["canon"] = (data.get("canon") or "")[:350]
    state["profiles"][uid]["locked"] = True

    save_data()
    emit_state_to_players()

@socketio.on("start_session")
def start_session(_=None):
    if request.sid not in admin_sids:
        emit("status_update", {"msg": "⚠️ 세션 시작은 마스터만 가능합니다."})
        return

    if state.get("session_started"):
        emit("status_update", {"msg": "ℹ️ 세션은 이미 시작된 상태입니다."})
        return

    if state.get("solo_mode"):
        if not state["profiles"]["user1"].get("locked"):
            emit("status_update", {"msg": "⚠️ 1인 모드에서는 Player 1의 프로필 저장(확정)이 필요합니다."})
            return
        state["session_started"] = True
        save_data()
        emit_state_to_players()
        emit("status_update", {"msg": "✅ 1인 모드로 세션이 시작되었습니다."}, broadcast=True)
        return

    p1_locked = bool(state["profiles"]["user1"].get("locked"))
    p2_locked = bool(state["profiles"]["user2"].get("locked"))

    if not p1_locked and not p2_locked:
        emit("status_update", {"msg": "⚠️ Player 1과 Player 2 모두 프로필 저장(확정)이 필요합니다."})
        return
    if not p1_locked:
        emit("status_update", {"msg": "⚠️ Player 1의 프로필 저장(확정)이 필요합니다."})
        return
    if not p2_locked:
        emit("status_update", {"msg": "⚠️ Player 2의 프로필 저장(확정)이 필요합니다."})
        return

    state["session_started"] = True
    save_data()
    emit_state_to_players()
    emit("status_update", {"msg": "✅ 2인 모드로 세션이 시작되었습니다."}, broadcast=True)

# =========================
# Socket: lorebook
# =========================
@socketio.on("add_lore")
def add_lore(data):
    idx = int(data.get("index", -1))
    title = (data.get("title","") or "")[:10]
    triggers = (data.get("triggers","") or "")
    content = (data.get("content","") or "")[:400]
    item = {"title": title, "triggers": triggers, "content": content}

    state.setdefault("lorebook", [])
    if 0 <= idx < len(state["lorebook"]):
        state["lorebook"][idx] = item
    else:
        state["lorebook"].append(item)

    save_data()
    emit_state_to_players()

@socketio.on("del_lore")
def del_lore(data):
    try:
        state["lorebook"].pop(int(data.get("index")))
        save_data()
        emit_state_to_players()
    except:
        pass

@socketio.on("reorder_lore")
def reorder_lore(data):
    try:
        f = int(data.get("from"))
        t = int(data.get("to"))
        lb = state.get("lorebook", [])
        if 0 <= f < len(lb) and 0 <= t < len(lb):
            item = lb.pop(f)
            lb.insert(t, item)
            save_data()
            emit_state_to_players()
    except:
        pass

# =========================
# Socket: reset
# =========================
@socketio.on("reset_session")
def reset_session(data):
    if str(data.get("password")) != str(state.get("admin_password")):
        emit("status_update", {"msg": "❌ 비밀번호가 일치하지 않습니다."})
        return

    state["ai_history"] = []
    state["lorebook"] = []
    state["summary"] = ""
    state["pending_inputs"] = {}
    typing_users.clear()

    state["session_started"] = False
    state["profiles"]["user1"]["locked"] = False
    state["profiles"]["user2"]["locked"] = False
    save_data()
    emit_state_to_players()

# =========================
# ✅ 합작: pending_inputs + 스킵 + 1회 출력
# =========================
def record_pending(uid: str, text: str):
    state.setdefault("pending_inputs", {})
    state["pending_inputs"][uid] = {
        "text": (text or "")[:600],
        "ts": datetime.now().isoformat(timespec="seconds")
    }
    save_data()

def both_ready() -> bool:
    if state.get("solo_mode"):
        return "user1" in state.get("pending_inputs", {})
    return ("user1" in state.get("pending_inputs", {})) and ("user2" in state.get("pending_inputs", {}))

@socketio.on("client_message")
def client_message(data):
    uid = data.get("uid")
    text = (data.get("text") or "").strip()

    if uid not in ("user1","user2"):
        return
    if connected_users.get(uid) != request.sid:
        return
    if not state.get("session_started", False):
        emit("status_update", {"msg": "⚠️ 세션이 아직 시작되지 않았습니다."})
        return

    record_pending(uid, text)
    typing_users.discard(uid)
    emit_state_to_players()

    if both_ready():
        try:
            trigger_ai_from_pending()
        except Exception as e:
            socketio.emit("status_update", {"msg": f"❌ 오류: {str(e)}"})
    else:
        other = "user2" if uid == "user1" else "user1"
        other_name = state["profiles"][other].get("name", other)
        socketio.emit("status_update", {"msg": f"⏳ {other_name} 입력을 기다리는 중... (스킵 가능)"})

@socketio.on("skip_turn")
def skip_turn(data):
    uid = data.get("uid")
    if uid not in ("user1","user2"):
        return
    if connected_users.get(uid) != request.sid:
        return
    if not state.get("session_started", False):
        return

    record_pending(uid, "(스킵)")
    typing_users.discard(uid)
    emit_state_to_players()

    if both_ready():
        try:
            trigger_ai_from_pending()
        except Exception as e:
            socketio.emit("status_update", {"msg": f"❌ 오류: {str(e)}"})
    else:
        other = "user2" if uid == "user1" else "user1"
        other_name = state["profiles"][other].get("name", other)
        socketio.emit("status_update", {"msg": f"⏳ {other_name} 입력을 기다리는 중... (스킵 가능)"})

def trigger_ai_from_pending():
    pending = state.get("pending_inputs", {})
    p1_text = pending.get("user1", {}).get("text", "(스킵)")
    p2_text = pending.get("user2", {}).get("text", "(스킵)")

    p1_name = state["profiles"]["user1"].get("name","Player 1")
    p2_name = state["profiles"]["user2"].get("name","Player 2")

    merged = f"{p1_text}\n{p2_text}"

    # 키워드 상위 3개
    active_context = []
    for l in state.get("lorebook", []):
        triggers = [t.strip() for t in (l.get("triggers","")).split(",") if t.strip()]
        if any(t in merged for t in triggers):
            active_context.append(f"[{l.get('title','')}]: {l.get('content','')}")
    active_context = active_context[:3]

    system_content = (
        f"{state.get('sys_prompt','')}\n\n"
        f"[현재 상황 요약]\n{state.get('summary','')}\n\n"
        f"[키워드 참고]\n" + "\n".join(active_context)
    )

    if would_overflow_context(system_content + merged):
        auto_summary_apply()
        system_content = (
            f"{state.get('sys_prompt','')}\n\n"
            f"[현재 상황 요약]\n{state.get('summary','')}\n\n"
            f"[키워드 참고]\n" + "\n".join(active_context)
        )

    round_block = (
        f"--- [ROUND INPUT] ---\n"
        f"<{p1_name}>: {p1_text}\n"
        f"<{p2_name}>: {p2_text}\n"
        f"--- [INSTRUCTION] ---\n"
        f"두 행동은 동시간대에 발생했다. 입력 순서와 무관하게 논리적으로 결합해 다음 장면을 서술하라.\n"
        f"분량은 공백 포함 2000자 내외로 풍성하게."
    )

    messages = [{"role":"system","content":system_content}]

    for ex in state.get("examples", []):
        if ex.get("q") and ex.get("a"):
            messages.append({"role":"user","content":ex["q"]})
            messages.append({"role":"assistant","content":ex["a"]})

    for h in build_history_block():
        messages.append({"role": "assistant" if h.startswith("**AI**") else "user", "content": h})

    messages.append({"role":"user","content": round_block})

    current_model = state.get("ai_model","gpt-5.2")
    socketio.emit("status_update", {"msg": f"🤔 {current_model} (합작) 응답 생성 중..."})

    if "gemini" in current_model.lower():
        if gemini_model is None:
            raise Exception("Gemini API 키가 없습니다.")
        
        # 안전 설정 정의 (성인용/폭력 묘사 허용 범위 최대화)
        from google.generativeai.types import HarmCategory, HarmBlockThreshold
        safety_settings = {
            # 성적인 내용은 차단 안 함
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            # 괴롭힘, 증오 발언, 위험 요소는 아주 심각한 것(HIGH)만 차단
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        prompt = system_content + "\n" + "\n".join(build_history_block()) + "\n" + round_block + "\nAI:"
        
        try:
            # 설정 적용하여 호출
            res = genai.GenerativeModel(current_model).generate_content(
                prompt, 
                safety_settings=safety_settings
            )
            ai_response = res.text
        except Exception as e:
            ai_response = f"⚠️ AI 응답 생성 중 오류가 발생했습니다. (필터링 또는 연결 오류: {str(e)})"

    state["ai_history"].append(f"**Round**: {p1_name}: {p1_text} / {p2_name}: {p2_text}")
    state["ai_history"].append(f"**AI**: {ai_response}")
    state["pending_inputs"] = {}
    save_data()

    socketio.emit("ai_typewriter_event", {"content": ai_response})
    emit_state_to_players()

# =========================
# HTML_TEMPLATE (완성본)
# =========================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>드림놀이</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.2/Sortable.min.js"></script>

  <style>
    :root{
      --bg: {{ theme.bg if theme else '#ffffff' }};
      --panel: {{ theme.panel if theme else '#f1f3f5' }};
      --accent: {{ theme.accent if theme else '#e91e63' }};
    }
    html,body{height:100%;margin:0;overflow:hidden;}
    body{font-family:Pretendard,sans-serif;display:flex;background:var(--bg);color:#000;}
    #main{flex:1;display:flex;flex-direction:column;height:100vh;border-right:1px solid rgba(0,0,0,0.05);min-width:0;}
    #chat-window{flex:1;overflow-y:auto;padding:30px 10%;display:flex;flex-direction:column;gap:15px;scroll-behavior:smooth;}
    #chat-content{display:flex;flex-direction:column;gap:15px;}
    #sidebar{width:320px;height:100vh;background:var(--panel);display:flex;flex-direction:column;overflow:hidden;}
    #sidebar-body{padding:20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;}
    #sidebar-footer{padding:12px 20px 16px;border-top:1px solid rgba(0,0,0,0.06);background:var(--panel);}

    textarea,input,select{background:var(--bg)!important;border:1px solid rgba(0,0,0,0.1)!important;border-radius:10px;padding:10px;width:100%;box-sizing:border-box;resize:none!important;}
    #msg-input{background:var(--panel)!important;border:1px solid rgba(0,0,0,0.15)!important;height:80px;}
    button{cursor:pointer;border:none;border-radius:8px;background:var(--accent);padding:10px;font-weight:bold;color:#fff;}
    button:hover{opacity:.85;}
    .btn-reset{background:#ff4444!important;}
    .master-btn{width:100%;background:transparent!important;color:#999!important;border:1px solid #ddd!important;padding:10px!important;border-radius:10px;font-weight:800;}

    .bubble{padding:15px 20px;border-radius:15px;max-width:85%;line-height:1.6;font-size:14px;white-space:pre-wrap;background:rgba(0,0,0,0.03);}
    .center-ai{align-self:center;background:var(--panel)!important;border-left:5px solid var(--accent);width:100%;max-width:800px;box-shadow:0 4px 15px rgba(0,0,0,0.05);}
    .user-bubble{align-self:center;background:#eee;color:#666;font-size:12px;padding:6px 12px;border-radius:20px;max-width:85%;}
    .name-tag{font-size:11px;color:#666;margin-bottom:6px;font-weight:700;}
    .typing-anim{animation:blink 1.4s infinite;}
    @keyframes blink{50%{opacity:.45;}}

    /* modal */
    #admin-modal{display:none;position:fixed;z-index:10000;left:0;top:0;width:100vw;height:100vh;background:rgba(0,0,0,0.6);backdrop-filter:blur(5px);align-items:center;justify-content:center;padding:24px;box-sizing:border-box;}
    .modal-content{width:100%;max-width:1200px;height:min(85vh,900px);background:#fff;border-radius:16px;display:flex;flex-direction:column;overflow:hidden;min-height:0;box-shadow:0 20px 60px rgba(0,0,0,0.3);}
    .modal-header{height:60px;flex:0 0 60px;display:flex;justify-content:space-between;align-items:center;padding:0 20px;background:#f8f9fa;border-bottom:1px solid #eee;box-sizing:border-box;}
    .tab-group{display:flex;gap:10px;height:100%;align-items:center;}
    .tab-btn{border:none;background:none!important;padding:0 14px;height:100%;font-size:14px;font-weight:700;color:#777;cursor:pointer;position:relative;}
    .tab-btn.active{color:var(--accent);}
    .tab-btn.active::after{content:"";position:absolute;bottom:0;left:0;width:100%;height:3px;background:var(--accent);}
    .close-btn{width:32px;height:32px;border-radius:50%;border:none;background:#eee;color:#000;font-size:16px;font-weight:800;cursor:pointer;padding:0;}
    .modal-body{flex:1;display:flex;overflow:hidden;min-height:0;}
    .tab-content{display:none;width:100%;height:100%;flex-direction:row;min-height:0;}
    .tab-content.active{display:flex;}
    .editor-side{flex:1.25;padding:20px;display:flex;flex-direction:column;gap:12px;overflow-y:auto;border-right:1px solid #f0f0f0;min-height:0;box-sizing:border-box;}
    .list-side{flex:.75;padding:20px;background:#fafafa;display:flex;flex-direction:column;gap:12px;overflow-y:auto;min-height:0;box-sizing:border-box;}
    .editor-side label,.list-side label{font-size:12px;font-weight:800;color:#999;text-transform:uppercase;}
    .save-btn{background:var(--accent);color:#fff;padding:14px;border-radius:10px;font-weight:800;border:none;}
    .fill-textarea{flex:1;min-height:260px;}
    .short-textarea{flex:none;height:160px;}
    .ex-block{background:#fff;border:1px solid #eee;padding:12px;border-radius:10px;display:flex;flex-direction:column;gap:8px;}
    .ex-block textarea{height:130px!important;}
    textarea::placeholder{color:#9aa0a6;font-weight:700;}

    /* tags */
    #tag-container{display:flex;flex-wrap:wrap;gap:8px;padding:10px;border:1px solid rgba(0,0,0,0.12);border-radius:10px;background:var(--bg);align-items:center;min-height:44px;box-sizing:border-box;}
    .tag-chip{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;background:rgba(0,0,0,0.06);border:1px solid rgba(0,0,0,0.08);font-size:12px;font-weight:700;user-select:none;}
    .tag-chip button{background:transparent!important;border:none;padding:0;cursor:pointer;color:#444;font-weight:900;}
    #tag-input{border:none!important;outline:none!important;background:transparent!important;width:220px!important;min-width:120px;padding:6px 8px!important;}

    /* lore list */
    .lore-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:10px;background:rgba(0,0,0,0.03);border:1px solid rgba(0,0,0,0.06);}
    .drag-handle{cursor:grab;color:#999;font-size:16px;user-select:none;}
    .lore-main{flex:1;min-width:0;}
    .lore-title{font-weight:800;font-size:13px;}
    .lore-trg{font-size:11px;color:#666;}
    .lore-actions{display:flex;gap:6px;}
    .mini-btn{padding:3px 7px;font-size:11px;border-radius:8px;}
    .mini-edit{background:#44aaff!important;}
    .mini-del{background:#ff4444!important;}

    /* ===== 전체 글씨 검정 통일 (override) ===== */
body, #main, #sidebar, #admin-modal, .modal-content,
h1,h2,h3,h4,h5,h6,p,span,div,label,
input,textarea,select,option{
  color:#000 !important;
}

/* placeholder도 검정 계열(조금 연하게는 유지 가능) */
textarea::placeholder, input::placeholder{
  color: rgba(0,0,0,0.45) !important;
  font-weight:700;
}

/* 모달 탭 버튼(비활성도 검정) */
.tab-btn{ color:#000 !important; opacity:0.7; }
.tab-btn.active{ opacity:1; }

/* 상태줄/이름표도 검정 */
#status, .name-tag, #role-display{ color:#000 !important; }

/* 말풍선 텍스트도 검정 */
.bubble, .user-bubble{ color:#000 !important; }

/* 링크 기본 파란색 방지(백업 저장 링크 등) */
a, a:visited { color:#000 !important; text-decoration:none; }
  </style>
</head>

<body>
  <div id="main">
    <div id="chat-window"><div id="chat-content"></div></div>

    <div id="input-area" style="padding:20px;background:var(--bg);">
      <div id="status" style="font-size:12px;margin-bottom:5px;color:var(--accent);font-weight:bold;">대기 중</div>

      <div style="display:flex;gap:10px;align-items:stretch;">
        <textarea id="msg-input" maxlength="600" placeholder="행동을 입력하세요..."></textarea>
        <div style="display:flex;flex-direction:column;gap:8px;width:110px;">
          <button id="send-btn" onclick="send()" style="width:110px;">전송</button>
          <button id="skip-btn" onclick="skipTurn()"
            style="width:110px;background:transparent;color:#666;border:1px solid rgba(0,0,0,0.2);
                   padding:6px 10px;font-size:12px;font-weight:800;">
            스킵
          </button>
        </div>
      </div>
    </div>
  </div>

  <div id="sidebar">
    <div id="sidebar-body">
      <h3>설정</h3>
      <div id="role-display" style="padding:10px;background:rgba(0,0,0,0.05);border-radius:8px;font-weight:800;color:#555;">접속 중...</div>

      <input type="text" id="p-name" maxlength="12" placeholder="이름">
      <textarea id="p-bio" maxlength="200" style="height:120px;" placeholder="캐릭터 설정(최대 200자)"></textarea>
      <textarea id="p-canon" maxlength="350" style="height:80px;" placeholder="관계 설정(최대 350자)"></textarea>

      <button onclick="saveProfile()" id="ready-btn">설정 저장</button>
      <div id="ready-status" style="font-size:11px;margin-top:5px;color:#666;">대기 중입니다...</div>
    </div>
    <div id="sidebar-footer">
      <button class="master-btn" onclick="requestAdmin()">마스터 설정</button>
    </div>
  </div>

  <div id="admin-modal">
    <div class="modal-content">
      <div class="modal-header">
        <div class="tab-group">
          <button class="tab-btn active" onclick="openTab(event,'t-base')">엔진</button>
          <button class="tab-btn" onclick="openTab(event,'t-story')">서사</button>
          <button class="tab-btn" onclick="openTab(event,'t-ex')">학습</button>
          <button class="tab-btn" onclick="openTab(event,'t-lore')">키워드</button>
        </div>
        <button onclick="closeModal(true)" class="close-btn">✕</button>
      </div>

      <div class="modal-body">

        <!-- 엔진 -->
        <div id="t-base" class="tab-content active">
          <div class="editor-side" style="display:flex;flex-direction:column;min-height:0;">
            <label>시스템 프롬프트 (최대 4000자)</label>
            <textarea id="m-sys" class="fill-textarea" maxlength="4000" style="flex:1;min-height:0;"></textarea>
            <button onclick="saveMaster()" class="save-btn" style="flex:0 0 auto;">저장</button>
          </div>

          <!-- 순서: 백업/복원 -> 요약 -> 모델 -> (맨 아래) 시작/초기화 -->
          <div class="list-side" style="display:flex;flex-direction:column;min-height:0;">
            <label>세션 설정 / 백업</label>

            <div style="display:flex;gap:6px;">
              <a href="/export" target="_blank" style="flex:1;">
                <button style="width:100%;background:#444!important;" class="mini-btn">백업 저장</button>
              </a>
              <button onclick="document.getElementById('import-file').click()" style="flex:1;background:#666!important;" class="mini-btn">복원</button>
              <input type="file" id="import-file" style="display:none;" accept=".json" onchange="uploadSessionFile(this)">
            </div>

            <textarea id="m-sum" class="short-textarea" maxlength="500" placeholder="현재 상황 요약(내부 기억용)"></textarea>

            <label>AI 모델 선택</label>
            <select id="m-ai-model">
              <option value="gpt-5.2">OpenAI GPT-5.2</option>
              <option value="gpt-4o">OpenAI GPT-4o</option>
              <option value="gemini-3-pro-preview">Google Gemini 3 Pro</option>
            </select>

            <label>1인 플레이 모드 (테스트용)</label>
            <select id="m-solo">
              <option value="false">사용 안 함(2인)</option>
              <option value="true">사용(1인)</option>
            </select>

            <div style="margin-top:auto; display:flex; gap:8px;">
              <button id="start-session-btn" onclick="startSession()" class="save-btn" style="background:#444!important; display:none; flex:1;">
                세션 시작
              </button>
              <button id="reset-session-btn" onclick="sessionReset()" class="btn-reset" style="display:none; flex:1;">
                세션 초기화
              </button>
            </div>
          </div>
        </div>

        <!-- 서사 -->
        <div id="t-story" class="tab-content">
          <div class="editor-side">
            <label>세션 제목 (최대 30자)</label>
            <input type="text" id="m-title" maxlength="30">
            <label>프롤로그 (최대 1000자)</label>
            <textarea id="m-pro" class="fill-textarea" maxlength="1000"></textarea>
            <button onclick="saveMaster()" class="save-btn">저장</button>
          </div>
          <div class="list-side">
            <label>안내</label>
            <p style="font-size:13px;color:#666;">프롬프트와 프롤로그가 모두 존재하면 모달 닫기 시 테마가 자동 분석됩니다.</p>
          </div>
        </div>

        <!-- 학습 -->
        <div id="t-ex" class="tab-content">
          <div class="editor-side">
            <label>말투 학습(예시 대화 3쌍, 각 500자)</label>
            <div class="ex-block">
              <label>Example 1</label>
              <textarea id="ex-q-0" maxlength="500" placeholder="질문"></textarea>
              <textarea id="ex-a-0" maxlength="500" placeholder="답변"></textarea>
            </div>
            <div class="ex-block">
              <label>Example 2</label>
              <textarea id="ex-q-1" maxlength="500" placeholder="질문"></textarea>
              <textarea id="ex-a-1" maxlength="500" placeholder="답변"></textarea>
            </div>
            <div class="ex-block">
              <label>Example 3</label>
              <textarea id="ex-q-2" maxlength="500" placeholder="질문"></textarea>
              <textarea id="ex-a-2" maxlength="500" placeholder="답변"></textarea>
            </div>
            <button onclick="saveExamples()" class="save-btn">저장</button>
          </div>
        </div>

        <!-- 키워드 -->
        <div id="t-lore" class="tab-content">
          <div class="editor-side" style="display:flex;flex-direction:column;min-height:0;">
            <label>키워드 이름 (최대 10자)</label>
            <input type="text" id="kw-t" maxlength="10">
            <label>트리거(최대 5개, Enter/Space)</label>
            <div id="tag-container" onclick="focusTagInput()"><input type="text" id="tag-input" placeholder="입력 후 Enter/Space"></div>
            <input type="hidden" id="tag-hidden" value="">
            <input type="hidden" id="kw-index" value="-1">
            <label>상세 설정 (최대 400자)</label>
            <textarea id="kw-c" class="fill-textarea" maxlength="400" style="flex:1;min-height:0;"></textarea>
            <button onclick="addLoreWithTags()" class="save-btn" style="flex:0 0 auto;">저장/수정</button>
          </div>
          <div class="list-side">
            <label>키워드 목록</label>
            <div id="lore-list" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;"></div>
          </div>
        </div>

      </div>
    </div>
  </div>

<script>
  const socket = io();
  let gState = null;
  let myRole = null;
  let tags = [];
  let sortable = null;
  let isTypewriter = false;

  function mdToSafeHtml(mdText){
    const raw = marked.parse(mdText || "");
    return DOMPurify.sanitize(raw, {USE_PROFILES: {html: true}});
  }

  // tags
  function focusTagInput(){ document.getElementById('tag-input')?.focus(); }
  function syncHidden(){ document.getElementById('tag-hidden').value = tags.join(','); }
  function renderTags(){
    const container = document.getElementById('tag-container');
    const input = document.getElementById('tag-input');
    if(!container || !input) return;
    [...container.querySelectorAll('.tag-chip')].forEach(el=>el.remove());
    tags.forEach((t, idx)=>{
      const chip = document.createElement('span');
      chip.className='tag-chip';
      chip.innerHTML = `<span>${t}</span>`;
      const x = document.createElement('button');
      x.textContent='×';
      x.onclick=(e)=>{e.stopPropagation(); tags.splice(idx,1); renderTags();};
      chip.appendChild(x);
      container.insertBefore(chip, input);
    });
    syncHidden();
  }
  function addTag(raw){
    const t = (raw||"").trim();
    if(!t) return;
    if(t.length>20) return alert("트리거는 20자 이내로 입력해주세요.");
    if(tags.length>=5) return alert("트리거는 최대 5개까지만 가능합니다.");
    if(tags.includes(t)) return;
    tags.push(t); renderTags();
  }
  function loadTagsFromString(s){
    tags=[]; (s||"").split(',').map(x=>x.trim()).filter(Boolean).forEach(x=>{ if(!tags.includes(x)) tags.push(x); });
    renderTags();
  }
  document.addEventListener('keydown', (e)=>{
    const ti = document.getElementById('tag-input');
    if(ti && document.activeElement===ti){
      if(e.key==='Enter' || e.key===' ' || e.key===','){ e.preventDefault(); addTag(ti.value); ti.value=''; }
      if(e.key==='Backspace' && ti.value==='' && tags.length>0){ tags.pop(); renderTags(); }
    }
  });
  function clearLoreEditor(){
    document.getElementById('kw-t').value="";
    document.getElementById('kw-c').value="";
    document.getElementById('kw-index').value="-1";
    tags=[]; renderTags();
    document.getElementById('tag-input').value="";
  }

  socket.on('connect', () => {
    // 브라우저에 저장된 내 역할이 있는지 확인해
    const savedRole = localStorage.getItem('dream_role');
    socket.emit('join_game', { saved_role: savedRole });
});

socket.on('assign_role', payload => {
    myRole = payload.role;
    // 역할을 부여받으면 브라우저에 저장! (새로고침 대비)
    if(myRole && myRole !== 'readonly') {
        localStorage.setItem('dream_role', myRole);
    }
  socket.on('reload_signal', ()=> window.location.reload());

  socket.on('assign_role', payload=>{
    myRole = payload.role;
    const roleEl = document.getElementById('role-display');

    if(payload.mode === 'readonly'){
      roleEl.innerText = "읽기 전용 모드(만석)";
      document.getElementById('msg-input').disabled = true;
      document.getElementById('send-btn').disabled = true;
      document.getElementById('skip-btn').disabled = true;
      return;
    }
    roleEl.innerText = (myRole==='user1') ? "Player 1 (당신)" : "Player 2 (당신)";
  });

  socket.on('status_update', d=>{
    const s = document.getElementById('status');
    s.innerHTML = d.msg;
    s.style.color = d.msg.includes('❌') ? 'red' : 'var(--accent)';
  });

  // typing effect
  socket.on('ai_typewriter_event', d=>{
    isTypewriter = true;
    const cc = document.getElementById('chat-content');
    const wrap = document.createElement('div');
    wrap.className = 'bubble center-ai';
    wrap.innerHTML = `<div class="name-tag">AI</div>`;
    cc.appendChild(wrap);

    const full = d.content || "";
    let i = 0;
    const tick = setInterval(()=>{
      i += 5;
      if(i > full.length) i = full.length;
      wrap.innerHTML = `<div class="name-tag">AI</div>` + mdToSafeHtml(full.slice(0, i));
      document.getElementById('chat-window').scrollTop = document.getElementById('chat-window').scrollHeight;
      if(i >= full.length){
        clearInterval(tick);
        isTypewriter = false;
      }
    }, 20);
  });

  socket.on('admin_auth_res', d=>{
    const ssb = document.getElementById('start-session-btn');
    const rsb = document.getElementById('reset-session-btn');
    if(d.success){
      document.getElementById('admin-modal').style.display='flex';
      document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
      document.getElementById('t-base').classList.add('active');
      document.querySelector('.tab-btn').classList.add('active');
      if(ssb) ssb.style.display = 'block';
      if(rsb) rsb.style.display = 'block';
      refreshUI();
    } else {
      if(ssb) ssb.style.display = 'none';
      if(rsb) rsb.style.display = 'none';
      alert("비밀번호가 일치하지 않습니다.");
    }
  });

  socket.on('initial_state', data=>{
    gState = data;

    if(data.theme){
      const root = document.documentElement.style;
      root.setProperty('--bg', data.theme.bg);
      root.setProperty('--panel', data.theme.panel);
      root.setProperty('--accent', data.theme.accent);
    }
    if(!isTypewriter) refreshUI();
  });

  function requestAdmin(){
    const pw = prompt("관리자 비밀번호를 입력하세요:");
    if(pw) socket.emit('check_admin', {password: pw});
  }

  function closeModal(maybeAnalyze){
    document.getElementById('admin-modal').style.display='none';
    if(maybeAnalyze){
      const sys = (document.getElementById('m-sys').value||"").trim();
      const pro = (document.getElementById('m-pro').value||"").trim();
      if(sys && pro) socket.emit('theme_analyze_request');
    }
  }
  function openTab(evt,id){
    document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    evt.currentTarget.classList.add('active');
  }

  // typing indicator emit
  const msgInput = document.getElementById('msg-input');
  let typingTimer = null;
  msgInput.addEventListener('input', ()=>{
    if(!myRole || myRole==='readonly') return;
    socket.emit('start_typing', {uid: myRole});
    clearTimeout(typingTimer);
    typingTimer = setTimeout(()=> socket.emit('stop_typing', {uid: myRole}), 1200);
  });

  function refreshUI(){
    if(!gState) return;

    const msg = document.getElementById('msg-input');
    const sendBtn = document.getElementById('send-btn');
    const skipBtn = document.getElementById('skip-btn');

    if(myRole==='readonly'){
      msg.disabled=true; sendBtn.disabled=true; skipBtn.disabled=true;
    } else {
      if(gState.session_started){
        msg.disabled=false; msg.placeholder="행동을 입력하세요...";
      } else {
        msg.disabled=true; msg.placeholder="프로필 확정 후 마스터가 세션을 시작합니다.";
      }
    }

    // chat render
    const cc = document.getElementById('chat-content');
    let html = `<div style="text-align:center;padding:20px;color:var(--accent);font-weight:bold;font-size:1.4em;">${gState.session_title}</div>`;
    html += `<div class="bubble center-ai"><div class="name-tag">PROLOGUE</div>${mdToSafeHtml(gState.prologue||"")}</div>`;

    (gState.ai_history||[]).forEach(m=>{
      if(m.startsWith("**AI**:")){
        html += `<div class="bubble center-ai"><div class="name-tag">AI</div>${mdToSafeHtml(m.replace("**AI**:","").trim())}</div>`;
      } else if(m.startsWith("**Round**:")){
        html += `<div class="user-bubble">${mdToSafeHtml(m.replace("**Round**:","").trim())}</div>`;
      } else {
        html += `<div class="user-bubble">${mdToSafeHtml(m)}</div>`;
      }
    });
    cc.innerHTML = html;
    document.getElementById('chat-window').scrollTop = document.getElementById('chat-window').scrollHeight;

    // status text
    const pends = (gState.pending_status||[]);
    const typers = (gState.typing_status||[]);
    const other = (myRole==='user1')?'user2':'user1';
    const otherName = gState.profiles?.[other]?.name || "상대";

    let st = "대기 중...";
    if(typers.includes(other)) st = `<span class="typing-anim">${otherName} 입력 중...</span>`;
    else if(pends.includes(other)) st = `✅ ${otherName} 입력 완료`;
    if(pends.includes(myRole)) st += " / 나도 완료";
    document.getElementById('status').innerHTML = st;

    // prevent duplicate submit
    const myDone = pends.includes(myRole);
    sendBtn.disabled = myDone || !gState.session_started || myRole==='readonly';
    skipBtn.disabled = myDone || !gState.session_started || myRole==='readonly';

    // profile restore
    const p = (myRole && gState.profiles && gState.profiles[myRole]) ? gState.profiles[myRole] : {name:"",bio:"",canon:"",locked:false};
    const activeId = document.activeElement?.id || "";
    if(activeId!=='p-name') document.getElementById('p-name').value = p.name || "";
    if(activeId!=='p-bio') document.getElementById('p-bio').value = p.bio || "";
    if(activeId!=='p-canon') document.getElementById('p-canon').value = p.canon || "";

    const locked = !!p.locked;
    const disableProfile = (myRole==='readonly') || locked;
    document.getElementById('p-name').readOnly = disableProfile;
    document.getElementById('p-bio').readOnly = disableProfile;
    document.getElementById('p-canon').readOnly = disableProfile;
    const rb = document.getElementById('ready-btn');
    rb.disabled = disableProfile;
    rb.innerText = locked ? "설정이 확정되었습니다" : "설정 저장";

    // admin restore
    if(activeId!=='m-title') document.getElementById('m-title').value = gState.session_title || "";
    if(activeId!=='m-sys') document.getElementById('m-sys').value = gState.sys_prompt || "";
    if(activeId!=='m-pro') document.getElementById('m-pro').value = gState.prologue || "";
    if(activeId!=='m-sum') document.getElementById('m-sum').value = gState.summary || "";
    document.getElementById('m-ai-model').value = gState.ai_model || "gpt-5.2";
    document.getElementById('m-solo').value = gState.solo_mode ? "true" : "false";

    if(gState.examples){
      for(let i=0;i<3;i++){
        const ex = gState.examples[i] || {};
        if(activeId!==`ex-q-${i}`) document.getElementById(`ex-q-${i}`).value = ex.q || "";
        if(activeId!==`ex-a-${i}`) document.getElementById(`ex-a-${i}`).value = ex.a || "";
      }
    }

    renderLoreList();
  }

  function send(){
    const t = document.getElementById('msg-input').value.trim();
    if(!t) return;
    
    // 🔥 추가: 즉시 버튼 잠그기
    document.getElementById('send-btn').disabled = true; 
    document.getElementById('msg-input').disabled = true;

    socket.emit('client_message', {uid: myRole, text: t});
    document.getElementById('msg-input').value='';
    socket.emit('stop_typing', {uid: myRole});
}

  function skipTurn(){
    if(!confirm("이번 턴을 스킵할까?")) return;
    socket.emit('skip_turn', {uid: myRole});
    socket.emit('stop_typing', {uid: myRole});
  }

  function saveProfile(){
    const name = document.getElementById('p-name').value;
    if(!name || name.includes("Player")) return alert("캐릭터 이름을 입력하세요.");
    if(confirm("이 설정으로 확정하시겠습니까? (확정 후 수정 불가)")){
      socket.emit('update_profile', {
        uid: myRole,
        name,
        bio: document.getElementById('p-bio').value,
        canon: document.getElementById('p-canon').value
      });
    }
  }

  function saveMaster(){
    socket.emit('save_master_base', {
      title: document.getElementById('m-title').value,
      sys: document.getElementById('m-sys').value,
      pro: document.getElementById('m-pro').value,
      sum: document.getElementById('m-sum').value,
      model: document.getElementById('m-ai-model').value,
      solo_mode: (document.getElementById('m-solo').value === "true")
    });
    alert("설정이 저장되었습니다.");
  }

  function startSession(){ socket.emit('start_session'); }
  function sessionReset(){
    if(confirm("정말로 초기화하시겠습니까?")){
      const pw = prompt("관리자 비밀번호를 입력하세요:");
      if(pw) socket.emit('reset_session', {password: pw});
    }
  }

  function saveExamples(){
    const exs = [];
    for(let i=0;i<3;i++){
      exs.push({ q: document.getElementById(`ex-q-${i}`).value, a: document.getElementById(`ex-a-${i}`).value });
    }
    socket.emit('save_examples', exs);
    alert("학습 데이터가 저장되었습니다.");
  }

  function addLoreWithTags(){
    const title = document.getElementById('kw-t').value;
    const content = document.getElementById('kw-c').value;
    const triggers = document.getElementById('tag-hidden').value;
    const idx = parseInt(document.getElementById('kw-index').value);
    if(!title) return alert("키워드 이름을 입력하세요.");
    if(!triggers) return alert("트리거 태그를 추가하세요.");
    if(!content) return alert("상세 설정을 입력하세요.");
    socket.emit('add_lore', {title, triggers, content, index: idx});
    clearLoreEditor();
  }

  function editLore(i){
    const l = gState.lorebook[i];
    document.getElementById('kw-t').value = l.title || "";
    document.getElementById('kw-c').value = l.content || "";
    document.getElementById('kw-index').value = i;
    loadTagsFromString(l.triggers || "");
  }
  function delLore(i){ socket.emit('del_lore', {index:i}); }

  function renderLoreList(){
    const list = document.getElementById('lore-list');
    if(!gState || !gState.lorebook) return;

    list.innerHTML = gState.lorebook.map((l,i)=>`
      <div class="lore-row" data-index="${i}">
        <div class="drag-handle">☰</div>
        <div class="lore-main">
          <div class="lore-title">${l.title}</div>
          <div class="lore-trg">${l.triggers}</div>
        </div>
        <div class="lore-actions">
          <button class="mini-btn mini-edit" onclick="editLore(${i})">수정</button>
          <button class="mini-btn mini-del" onclick="delLore(${i})">삭제</button>
        </div>
      </div>
    `).join('');

    if(sortable) sortable.destroy();
    sortable = new Sortable(list, {
      handle: '.drag-handle',
      animation: 120,
      onEnd: (evt) => {
        if(evt.oldIndex === evt.newIndex) return;
        socket.emit('reorder_lore', {from: evt.oldIndex, to: evt.newIndex});
      }
    });
  }

  function uploadSessionFile(input){
    if(!input.files || !input.files[0]) return;
    const formData = new FormData();
    formData.append('file', input.files[0]);
    fetch('/import',{method:'POST',body:formData})
      .then(res=>{ if(res.ok) alert("복원되었습니다."); else alert("복원 실패"); input.value=''; })
      .catch(err=>alert("업로드 오류: "+err));
  }
</script>
</body>
</html>
"""

# =========================
# Run
# =========================
if __name__ == "__main__":
    print(f"✅ Running on http://{HOST}:{PORT}")
    socketio.run(app, host=HOST, port=PORT, debug=False, allow_unsafe_werkzeug=True)
