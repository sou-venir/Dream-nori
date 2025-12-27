
import urllib.parse
import os, json, copy, re
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, render_template_string, request, Response
from flask_socketio import SocketIO, emit
import openai
import google.generativeai as genai
from dotenv import load_dotenv  # 추가! (.env 파일 읽기용)

load_dotenv() # 추가!



# =========================
# Drive & Storage
# =========================
SAVE_PATH = os.path.join(os.getcwd(), 'data')
os.makedirs(SAVE_PATH, exist_ok=True)
DATA_FILE = os.path.join(SAVE_PATH, "save_data.json")

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            state_to_save = copy.deepcopy(state)
            state_to_save["client_map"] = client_map
            json.dump(state_to_save, f, ensure_ascii=False, indent=2)
    except: pass

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: return None
    return None

# =========================
# Keys & AI setup
# =========================
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

gemini_model = None
try:
    OPENAI_API_KEY = userdata.get('OPENAI_API_KEY')
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    try:
        GEMINI_API_KEY = userdata.get('GEMINI_API_KEY')
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-3-pro-preview')
    except: pass
except Exception as e:
    print(f"❌ 설정 오류: {e}")

# =========================
# App
# =========================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*")

# =========================
# State
# =========================
initial_state = {
    "session_title": "드림놀이",
    "theme": {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"},
    "ai_model": "gpt-5.2",
    "admin_password": ADMIN_PASSWORD,
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

saved_data = load_data()
if saved_data:
    state = saved_data
    # 기존 ip_map은 버리고 client_map(고유 ID용) 사용
    state.pop("ip_map", None)
    client_map = state.pop("client_map", {})
else:
    state = copy.deepcopy(initial_state)
    client_map = {}


connected_users = {"user1": None, "user2": None}
readonly_sids = set()
admin_sids = set()
typing_users = set()

# =========================
# Helpers
# =========================
def sanitize_filename(name: str) -> str:
    name = (name or "session").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
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
        if k in data: state[k] = copy.deepcopy(data[k])

def get_sanitized_state():
    safe = copy.deepcopy(state)
    safe["profiles"]["user1"]["bio"] = ""
    safe["profiles"]["user1"]["canon"] = ""
    safe["profiles"]["user2"]["bio"] = ""
    safe["profiles"]["user2"]["canon"] = ""
    return safe

def emit_state_to_players(save=True):
    if save: save_data()
    payload = get_sanitized_state()
    payload["pending_status"] = list(state.get("pending_inputs", {}).keys())
    payload["typing_status"] = list(typing_users)

    # User1, User2에게 전송
    if connected_users["user1"]: socketio.emit("initial_state", payload, room=connected_users["user1"])
    if connected_users["user2"]: socketio.emit("initial_state", payload, room=connected_users["user2"])
    # 관전자에게도 전송
    for rsid in readonly_sids:
        socketio.emit("initial_state", payload, room=rsid)

# (1) ✅ analyze_theme_color() 함수 전체를 이걸로 교체 (OpenAI 전용 + 들여쓰기 정상)

def analyze_theme_color(title, sys_prompt):
    prompt_text = (
        f"세션 제목: {title}\n"
        f"시스템/프롤로그: {sys_prompt[:1200]}\n\n"
        "웹 UI 컬러 팔레트 전문가입니다.\n"
        "텍스트는 항상 검정(#000000)입니다. bg/panel은 매우 밝고 대비가 높아야 합니다.\n"
        "accent는 시나리오 분위기를 대표해야 합니다.\n"
        "반드시 JSON만 반환: {\"bg\":\"#RRGGBB\",\"panel\":\"#RRGGBB\",\"accent\":\"#RRGGBB\"}"
    )
    try:
        if not client:
            return state.get("theme")

        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt_text}
            ],
            response_format={"type": "json_object"}
        )
        obj = json.loads(res.choices[0].message.content)

        out = state.get("theme", {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"})
        for k in ("bg", "panel", "accent"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("#") and len(v) == 7:
                out[k] = v
        return out

    except Exception as e:
        print(f"⚠️ 테마 분석 실패(OpenAI): {e}")
        return state.get("theme")

# Context / Summary
MAX_CONTEXT_CHARS_BUDGET = 14000
HISTORY_SOFT_LIMIT_CHARS = 9500
SUMMARY_MAX_CHARS = 500
TARGET_MAX_TOKENS = 1100

def build_history_block():
    history = state.get("ai_history", [])
    collected = []
    total = 0
    for msg in reversed(history):
        add_len = len(msg) + 1
        if total + add_len > HISTORY_SOFT_LIMIT_CHARS: break
        collected.append(msg)
        total += add_len
    collected.reverse()
    return collected

def would_overflow_context(extra_incoming: str) -> bool:
    sys_p = state.get("sys_prompt","")
    pro = state.get("prologue","")
    summ = state.get("summary","")
    hist = "\n".join(build_history_block())
    return (len(sys_p)+len(pro)+len(summ)+len(hist)+len(extra_incoming)+2000) > MAX_CONTEXT_CHARS_BUDGET

def auto_summary_apply():
    def run_once():
        recent_log = "\n".join(state.get("ai_history", [])[-60:])
        if not recent_log: return None
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":f"요약해줘:\n{recent_log}"}]
        )
        return (res.choices[0].message.content or "").strip()
    try:
        s = run_once()
        if s: state["summary"] = s[:SUMMARY_MAX_CHARS]; save_data()
    except: pass

# =========================
# Routes
# =========================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, theme=state.get("theme"))

#여기까지 삭제

@app.route("/export")
def export_config():
    cfg = get_export_config_only()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    safe_title = re.sub(r"[^A-Za-z0-9_-]+", "_", (cfg.get("session_title") or "session"))
    fname = f"{safe_title}_{ts}.json"
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    resp = Response(data, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(fname)
    return resp

@app.route("/import", methods=["POST"])
def import_config():
    try:
        if "file" not in request.files: return "파일X", 400
        file = request.files["file"]
        if file.filename == "": return "파일X", 400
        data = json.load(file)
        import_config_only(data)
        save_data()
        emit_state_to_players()
        return "OK", 200
    except Exception as e: return str(e), 500

# =========================
# Socket Logic (Fixed Session Restoration)
# =========================
@socketio.on("join_game")
def join_game(data=None):
    sid = request.sid
    # 프론트엔드에서 보낸 고유 ID (UUID)
    cid = (data or {}).get("client_id")

    # 1. 이 ID가 이미 역할을 가지고 있는지 확인
    if cid in client_map:
        role = client_map[cid]
        connected_users[role] = sid
        emit("assign_role", {"role": role, "mode": "player", "source": "uuid"})
        emit_state_to_players()
        return

    # 2. 빈 자리 찾기
    target_role = None
    if connected_users["user1"] is None: target_role = "user1"
    elif connected_users["user2"] is None: target_role = "user2"

    if target_role:
        connected_users[target_role] = sid
        client_map[cid] = target_role # ID와 역할 매핑 저장
        save_data()
        emit("assign_role", {"role": target_role, "mode": "player", "source": "new"})
        emit_state_to_players()
        return

    # 3. 만석
    readonly_sids.add(sid)
    emit("assign_role", {"role": "readonly", "mode": "readonly"})
    emit_state_to_players()


# ✅ disconnect는 이 블록 하나만 남겨. (중복된 disconnect 데코레이터/함수는 삭제)
@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    admin_sids.discard(sid)

    for role in ("user1", "user2"):
        if connected_users[role] == sid:
            connected_users[role] = None
            typing_users.discard(role)
            state.get("pending_inputs", {}).pop(role, None)

    readonly_sids.discard(sid)
    save_data()
    emit_state_to_players()

@socketio.on("clear_all_roles")
def clear_all_roles(data):
    if str(data.get("password")) != str(state.get("admin_password")): return
    
    global client_map
    client_map = {} # 모든 장치-역할 매핑 삭제
    for role in connected_users:
        connected_users[role] = None
    
    save_data()
    socketio.emit("reload_signal") # 모든 클라이언트 새로고침 시켜서 재접속 유도


@socketio.on("start_typing")
def start_typing(data):
    uid = data.get("uid")
    if uid in ("user1","user2"):
        typing_users.add(uid)
        # broadcast=True 삭제
        socketio.emit("typing_update", {"typing_users": list(typing_users)})

@socketio.on("stop_typing")
def stop_typing(data):
    uid = data.get("uid")
    if uid in ("user1","user2"):
        typing_users.discard(uid)
        # broadcast=True 삭제
        socketio.emit("typing_update", {"typing_users": list(typing_users)})

@socketio.on("edit_history_msg")
def edit_history_msg(data):
    try:
        idx = int(data.get("index"))
        text = data.get("text")
        if 0 <= idx < len(state["ai_history"]):
            # 기존 태그(**AI**: 등)가 사라지지 않게 처리할 수도 있지만,
            # 여기서는 클라이언트가 보내준 전체 텍스트로 교체
            state["ai_history"][idx] = text
            save_data()
            emit_state_to_players()
    except: pass

@socketio.on("check_admin")
def check_admin(data):
    ok = str(data.get("password")) == str(state.get("admin_password"))
    if ok: admin_sids.add(request.sid)
    emit("admin_auth_res", {"success": ok})

@socketio.on("save_master_base")
def save_master_base(data):
    state["session_title"] = (data.get("title", state["session_title"]) or "")[:30]
    state["sys_prompt"] = (data.get("sys", state["sys_prompt"]) or "")[:4000]
    state["prologue"] = (data.get("pro", state["prologue"]) or "")[:1000]
    state["summary"] = (data.get("sum", state["summary"]) or "")[:SUMMARY_MAX_CHARS]
    state["ai_model"] = data.get("model", state.get("ai_model","gpt-5.2"))
    
    # 1인 모드 문자열 변환 버그 수정
    val = data.get("solo_mode")
    if val is not None:
        state["solo_mode"] = (str(val).lower() == "true")
        
    save_data()
    emit_state_to_players()

# 프로필 잠금 해제 기능 추가
@socketio.on("unlock_profile")
def unlock_profile(data):
    if str(data.get("password")) != str(state.get("admin_password")): return
    target = data.get("target") # "user1" 또는 "user2"
    if target in state["profiles"]:
        state["profiles"][target]["locked"] = False
        save_data()
        emit_state_to_players()

@socketio.on("theme_analyze_request")
def theme_analyze_request(_=None):
    if not (state.get("sys_prompt","").strip() and state.get("prologue","").strip()):
        return
    # prologue까지 합쳐서 분석 품질 올리기
    combined = state.get("sys_prompt","") + "\n\n[PROLOGUE]\n" + state.get("prologue","")
    state["theme"] = analyze_theme_color(state.get("session_title",""), combined)
    save_data()
    emit_state_to_players()


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
    if uid not in ("user1","user2"): return
    if connected_users.get(uid) != request.sid: return
    if state["profiles"][uid].get("locked"): return
    name = (data.get("name") or "").strip()
    if not name: return
    state["profiles"][uid]["name"] = name[:12]
    state["profiles"][uid]["bio"] = (data.get("bio") or "")[:200]
    state["profiles"][uid]["canon"] = (data.get("canon") or "")[:350]
    state["profiles"][uid]["locked"] = True
    save_data()
    emit_state_to_players()

@socketio.on("start_session")
def start_session(_=None):
    if request.sid not in admin_sids: return
    if state.get("session_started"): return
    state["session_started"] = True
    save_data()
    emit_state_to_players()
    emit("status_update", {"msg": "✅ 세션이 시작되었습니다!"}, broadcast=True)

@socketio.on("add_lore")
def add_lore(data):
    idx = int(data.get("index", -1))
    title = (data.get("title","") or "")[:20]
    triggers = (data.get("triggers","") or "")
    content = (data.get("content","") or "")[:400]
    item = {"title": title, "triggers": triggers, "content": content}
    state.setdefault("lorebook", [])
    if (idx < 0 or idx >= len(state["lorebook"])) and len(state["lorebook"]) >= 20:
        emit("status_update", {"msg": "⚠️ 키워드북은 최대 20개까지 가능합니다."})
        return
    if 0 <= idx < len(state["lorebook"]): state["lorebook"][idx] = item
    else: state["lorebook"].append(item)
    save_data()
    emit_state_to_players()

@socketio.on("del_lore")
def del_lore(data):
    try: state["lorebook"].pop(int(data.get("index"))); save_data(); emit_state_to_players()
    except: pass

@socketio.on("reorder_lore")
def reorder_lore(data):
    try:
        f, t = int(data.get("from")), int(data.get("to"))
        state["lorebook"].insert(t, state["lorebook"].pop(f))
        save_data(); emit_state_to_players()
    except: pass

# (서버) reset_session 이벤트를 아래로 교체
@socketio.on("reset_session")
def reset_session(data):
    if str(data.get("password")) != str(state.get("admin_password")):
        emit("status_update", {"msg": "❌ 비밀번호가 일치하지 않습니다."})
        return

    # 1) 상태 전부 초기화(화면에 보이는 모든 입력값이 비게)
    state["session_title"] = "드림놀이"
    state["theme"] = {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"}
    state["ai_model"] = "gpt-5.2"
    state["solo_mode"] = False
    state["session_started"] = False

    state["profiles"]["user1"] = {"name": "Player 1", "bio": "", "canon": "", "locked": False}
    state["profiles"]["user2"] = {"name": "Player 2", "bio": "", "canon": "", "locked": False}

    state["pending_inputs"] = {}
    typing_users.clear()

    state["ai_history"] = []
    state["summary"] = ""
    state["prologue"] = ""
    state["sys_prompt"] = "당신은 숙련된 TRPG 마스터입니다."

    state["lorebook"] = []
    state["examples"] = [{"q": "", "a": ""}, {"q": "", "a": ""}, {"q": "", "a": ""}]

    save_data()
    emit_state_to_players()
    emit("status_update", {"msg": "🧹 세션이 완전히 초기화되었습니다."}, broadcast=True)

def record_pending(uid, text):
    state.setdefault("pending_inputs", {})
    state["pending_inputs"][uid] = {"text": (text or "")[:600], "ts": datetime.now().isoformat()}
    save_data()

def both_ready():
    if state.get("solo_mode"): return "user1" in state.get("pending_inputs", {})
    return "user1" in state.get("pending_inputs", {}) and "user2" in state.get("pending_inputs", {})

def trigger_ai_from_pending():
    pending = state.get("pending_inputs", {})
    p1_text = pending.get("user1", {}).get("text", "(스킵)")
    p2_text = pending.get("user2", {}).get("text", "(스킵)")
    
    # [1] 플레이어 설정 데이터 가져오기
    u1 = state["profiles"]["user1"]
    u2 = state["profiles"]["user2"]
    p1_name, p1_bio, p1_canon = u1.get("name", "Player 1"), u1.get("bio", ""), u1.get("canon", "")
    p2_name, p2_bio, p2_canon = u2.get("name", "Player 2"), u2.get("bio", ""), u2.get("canon", "")

    # [2] 로어(키워드) 매칭
    merged = f"{p1_text}\n{p2_text}"
    active_context = []
    for l in state.get("lorebook", []):
        triggers = [t.strip() for t in (l.get("triggers","")).split(",") if t.strip()]
        if any(t in merged for t in triggers):
            active_context.append(f"[{l.get('title','')}]: {l.get('content','')}")
    active_context = active_context[:3]

    # [3] 프롬프트 조각 준비 (제목, 프롤로그, 프로필)
    session_title = state.get("session_title", "Untitled Session")
    sys_prompt = state.get('sys_prompt','')
    prologue_text = state.get("prologue", "")

    profile_content = (
        f"### [CHARACTER PROFILES]\n"
        f"1. {p1_name}\n- Bio: {p1_bio}\n- Relationship/Canon: {p1_canon}\n\n"
        f"2. {p2_name}\n- Bio: {p2_bio}\n- Relationship/Canon: {p2_canon}"
    )

    # [4] 전체 시스템 프롬프트 조립 (Helper 함수)
    def build_full_system_content():
        return (
            f"### [Session Title]: {session_title}\n\n"
            f"{sys_prompt}\n\n"
            f"{profile_content}\n\n"
            f"### [PROLOGUE]\n{prologue_text}\n\n"
            f"### [Current Summary]\n{state.get('summary','')}\n\n"
            f"### [Active Lore]\n" + "\n".join(active_context)
        )

    system_content = build_full_system_content()
    
    # [5] 컨텍스트 오버플로우 체크 및 재조립
    if would_overflow_context(system_content + merged):
        auto_summary_apply()
        system_content = build_full_system_content()

    # [6] 답변 직전 강조 지침 (Recency Bias 활용)
    priority_instruction = (
        f"!!! [CRITICAL AUTHORITY] !!!\n"
        f"위의 [Session Title], [PROLOGUE], [CHARACTER PROFILES] 설정을 완벽히 숙지하십시오. "
        f"이전 대화보다 마스터의 지침과 캐릭터 설정을 최우선으로 하여 서술하십시오.\n\n"
        f"[MANDATORY RULE]:\n{sys_prompt}"
    )

    round_block = f"--- [ROUND INPUT] ---\n<{p1_name}>: {p1_text}\n<{p2_name}>: {p2_text}\n--- [INSTRUCTION] ---\n두 행동은 동시간대입니다. 캐릭터 설정을 준수하여 2000자 내외로 서술하세요."

    # [7] 메시지 리스트 구성
    messages = [{"role":"system","content":system_content}]
    
    # 예시 대화 (Few-shot)
    for ex in state.get("examples", []):
        if ex.get("q"): messages.extend([{"role":"user","content":ex["q"]}, {"role":"assistant","content":ex["a"]}])
    
    # 히스토리
    for h in build_history_block():
        messages.append({"role": "assistant" if h.startswith("**AI**") else "user", "content": h})
    
    # 이번 라운드 입력 + 강제 지침
    messages.append({"role":"user","content": round_block})
    messages.append({"role": "system", "content": priority_instruction})

    current_model = state.get("ai_model","gpt-5.2")
    socketio.emit("status_update", {"msg": f"🤔 {current_model} 집필 중..."})

    ai_response = ""
    try:
        if "gemini" in current_model.lower() and gemini_model:
            from google.generativeai.types import HarmCategory, HarmBlockThreshold
            safe = {HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE}
            # 제미나이용 프롬프트 조합
            prompt = system_content + "\n" + "\n".join(build_history_block()) + "\n" + round_block + "\n\n" + priority_instruction
            ai_response = gemini_model.generate_content(prompt, safety_settings=safe).text
        elif client:
            res = client.chat.completions.create(model=current_model, messages=messages, max_tokens=TARGET_MAX_TOKENS)
            ai_response = res.choices[0].message.content
        else:
            ai_response = "API Key Error."
    except Exception as e:
        ai_response = f"Error: {e}"

    state["ai_history"].append(f"**Round**: {p1_name}: {p1_text} / {p2_name}: {p2_text}")
    state["ai_history"].append(f"**AI**: {ai_response}")
    state["pending_inputs"] = {}
    save_data()

    socketio.emit("ai_typewriter_event", {"content": ai_response})
    emit_state_to_players()
    
@socketio.on("client_message")
def client_message(data):
    uid, text = data.get("uid"), (data.get("text") or "").strip()
    if uid not in ("user1","user2") or not state.get("session_started"): return
    record_pending(uid, text)
    typing_users.discard(uid)
    emit_state_to_players()
    if both_ready(): trigger_ai_from_pending()
    else:
        other = "user2" if uid == "user1" else "user1"
        nm = state["profiles"][other].get("name", other)
        socketio.emit("status_update", {"msg": f"⏳ {nm}님 입력 대기... (스킵 가능)"})

@socketio.on("skip_turn")
def skip_turn(data):
    uid = data.get("uid")
    if uid not in ("user1","user2") or not state.get("session_started"): return
    record_pending(uid, "(스킵)")
    typing_users.discard(uid)
    emit_state_to_players()
    if both_ready(): trigger_ai_from_pending()
    else:
        other = "user2" if uid == "user1" else "user1"
        nm = state["profiles"][other].get("name", other)
        socketio.emit("status_update", {"msg": f"⏳ {nm}님 입력 대기... (스킵 가능)"})

# =========================
# HTML Template
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
    #chat-content{display:flex;flex-direction:column;gap:15px;padding-bottom:20px;}
    #sidebar{width:320px;height:100vh;background:var(--panel);display:flex;flex-direction:column;overflow:hidden;}
    #sidebar-body{padding:20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;}
    #sidebar-footer{padding:12px 20px 16px;border-top:1px solid rgba(0,0,0,0.06);background:var(--panel);}

    textarea,input,select{background:var(--bg)!important;border:1px solid rgba(0,0,0,0.1)!important;border-radius:10px;padding:10px;width:100%;box-sizing:border-box;resize:none!important;}
    #msg-input{background:var(--panel)!important;border:1px solid rgba(0,0,0,0.15)!important;height:80px;}
    button{cursor:pointer;border:none;border-radius:8px;background:var(--accent);padding:10px;font-weight:bold;color:#fff;}
    button:hover{opacity:.85;}
    .btn-reset{background:#ff4444!important;}
    .master-btn{width:100%;background:transparent!important;color:#999!important;border:1px solid #ddd!important;padding:10px!important;border-radius:10px;font-weight:800;}

    /* [추가] 말풍선 좌우 정렬 스타일 */
    .bubble {
        padding: 8px 14px; /* 패딩을 조금 줄임 */
        border-radius: 15px;
        max-width: 85%;
        width: fit-content;
        text-align: left;
        line-height: 1.5; /* 줄간격 약간 조정 */
        font-size: 14px;
        white-space: pre-wrap;
        position: relative;
        word-wrap: break-word;
    }

    /* [추가] 마크다운 때문에 생기는 불필요한 위아래 여백 제거 */
    .bubble p {
        margin: 0;
    }
    .bubble pre {
  display: block;
  background: #282c34;
  color: #abb2bf;
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
  font-family: 'Consolas', 'Monaco', monospace;
  white-space: pre; /* 줄바꿈 유지 */
  box-shadow: inset 0 0 10px rgba(0,0,0,0.2);
}
    .bubble code {
  background: rgba(0,0,0,0.08);
  padding: 2px 4px;
  border-radius: 3px;
  font-family: monospace;
  font-size: 0.9em;
}
    .bubble pre code {
  background: transparent;
  padding: 0;
  color: inherit;
}
    .bubble em, .bubble i {
        font-style: italic;
        color: inherit !important; /* 원래 글자색 따라감 */
    }
    .bubble blockquote {
        border-left: 4px solid var(--accent); /* 포인트 컬러로 옆줄 그어줌 */
        margin: 10px 0;
        padding: 5px 15px;
        background: rgba(0,0,0,0.03); /* 아주 살짝 배경색 깔아줌 */
        font-style: italic; /* 기울임꼴로 분위기 있게 */
        color: rgba(0,0,0,0.7); /* 글자색은 살짝 투명하게 */
    }

    /* 오른쪽 말풍선(내꺼) 안의 인용구는 줄 색상을 하얗게 */
    .align-right blockquote {
        border-left: 4px solid rgba(255,255,255,0.5);
        background: rgba(255,255,255,0.1);
        color: rgba(255,255,255,0.9) !important;
    }
    .edit-btn {
  background: transparent;
  border: 1px solid rgba(0,0,0,0.1);
  color: #999;
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 8px;
  cursor: pointer;
}
    .edit-btn:hover {
  background: rgba(0,0,0,0.05);
  color: var(--accent);
}

    .align-left { align-self: flex-start; background: rgba(0,0,0,0.04); color: #000; border-top-left-radius: 2px; }
    .align-left .name-tag { color: #666; font-size: 11px; font-weight: bold; margin-bottom: 4px; }

    .align-right { align-self: flex-end; background: var(--accent); color: #fff !important; border-top-right-radius: 2px; }
    .align-right .name-tag { color: rgba(255,255,255,0.8); text-align: right; font-size: 11px; font-weight: bold; margin-bottom: 4px; }
    .align-right p, .align-right span { color: #fff !important; }

    .center-ai {
    align-self: center; /* 플렉스박스에서 가운데 배치 */
    background: var(--panel) !important;
    border-left: 5px solid var(--accent);

    width: fit-content;
    max-width: 90%;

    box-shadow: 0 4px 15px rgba(0,0,0,0.05);
}
    .center-ai .name-tag { font-weight:900; color:var(--accent); }

    .typing-anim{animation:blink 1.4s infinite;}
    @keyframes blink{50%{opacity:.45;}}

    /* [추가] 글자수 카운터 스타일 */
    .char-cnt { font-size: 10px; color: #888; text-align: right; margin-top: 2px; }

    /* [요청] 입력창 높이 수정 */
    #p-bio { height: 160px !important; }   /* 캐릭터 설정 */
    #p-canon { height: 180px !important; } /* 관계 설정 (제일 크게) */

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
    /* profile overlay fix (A: 입력 3개만 덮기) */
    #profile-wrap {
  position: relative; /* 오버레이 가두기 */
  z-index: 0;         /* 스태킹 컨텍스트 생성 (뚫림 방지) */
  overflow: hidden;   /* 튀어나감 방지 */
  border-radius: 12px;
  /* 여백이 없으면 오버레이가 너무 빡빡해 보일 수 있음. 살짝 줌 */
  padding: 2px;
}

    #profile-lock-overlay {
  position: absolute;
  inset: 0;
  z-index: 100;

  /* 여기를 투명으로 변경! */
  background: transparent;

  border-radius: 12px;
  cursor: not-allowed; /* 마우스 올리면 '금지' 표시는 뜨게 유지 */
}

   #t-lore.tab-content {
    height: 100%;
    overflow: hidden; /* 전체 스크롤 방지 */
}

#t-lore .editor-side {
    flex: 1.2;
    display: flex;
    flex-direction: column;
    gap: 8px;
    height: 100%;
    overflow: hidden;
}

#t-lore .list-side {
    flex: 0.8;
    display: flex;
    flex-direction: column;
    gap: 10px;
    height: 100%;
    overflow: hidden;
    background: #fafafa;
}
    #lore-list {
    flex: 1;
    overflow-y: auto;
    padding-right: 5px;
}

/* 상세 설정 입력창이 남은 모든 공간 차지 */
#kw-c {
    flex: 1;
    min-height: 0 !important; /* flex-grow를 위해 필요 */
    height: auto !important;
}
    /* Override */
    body, #main, #sidebar, #admin-modal, .modal-content, h1,h2,h3,h4,h5,h6,p,span,div,label,input,textarea,select,option{ color:#000 !important; }
    textarea::placeholder, input::placeholder{ color: rgba(0,0,0,0.45) !important; font-weight:700; }
    .tab-btn{ color:#000 !important; opacity:0.7; } .tab-btn.active{ opacity:1; }
    #status, .name-tag, #role-display{ color:#000 !important; }
    a, a:visited { color:#000 !important; text-decoration:none; }
    input:disabled::placeholder, textarea:disabled::placeholder {
  color: transparent !important;
}
    .edit-mode-wrap {
        width: 100%;
        min-width: 300px;
        display: flex;
        flex-direction: column;
        gap: 5px;
    }
    .edit-mode-textarea {
        width: 100%;
        height: 150px;
        background: #fff;
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 10px;
        font-family: inherit;
        font-size: 14px;
        line-height: 1.5;
        resize: vertical;
    }
    .edit-actions {
        display: flex;
        gap: 5px;
        justify-content: flex-end;
    }


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
          <button id="skip-btn" onclick="skipTurn()" style="width:110px;background:transparent;color:#666;border:1px solid rgba(0,0,0,0.2);padding:6px 10px;font-size:12px;font-weight:800;">스킵</button>
        </div>
      </div>
    </div>
  </div>

  <div id="sidebar">
    <div id="sidebar-body">
      <h3>설정 <span id="role-badge" style="font-size:12px;color:var(--accent)"></span></h3>
      <div id="role-display" style="padding:10px;background:rgba(0,0,0,0.05);border-radius:8px;font-weight:800;color:#555;">접속 중...</div>

      <!-- 여기가 핵심: 잠금 기능을 위한 포장지 -->
      <div id="profile-wrap">
        <div id="profile-lock-overlay" style="display:none;"></div>

        <input type="text" id="p-name" maxlength="12" placeholder="이름">

        <div>
          <textarea id="p-bio" maxlength="200" oninput="upCnt(this)" placeholder="캐릭터 설정 (최대 200자)"></textarea>
          <div id="cnt-p-bio" class="char-cnt">0/200</div>
        </div>

        <div>
          <textarea id="p-canon" maxlength="350" oninput="upCnt(this)" placeholder="드림캐 설정 (최대 350자)"></textarea>
          <div id="cnt-p-canon" class="char-cnt">0/350</div>
        </div>
      </div>
      <!-- //profile-wrap 끝 -->

      <button onclick="saveProfile()" id="ready-btn">설정 저장</button>
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
            <textarea id="m-sys" class="fill-textarea" maxlength="4000" oninput="upCnt(this)" style="flex:1;min-height:0;"></textarea>
            <div id="cnt-m-sys" class="char-cnt">0/4000</div>
            <button onclick="saveMaster()" class="save-btn" style="flex:0 0 auto;">저장</button>
          </div>
          <div class="list-side" style="display:flex;flex-direction:column;min-height:0; gap:12px;">
    <label>서버 관리</label>
    <button onclick="clearRoles()" style="width:100%; background:#ff9800 !important; color:white; font-weight:800;" class="mini-btn">접속 권한 전체 초기화</button>
    <div style="display:flex; gap:4px;">
    <button onclick="unlockProfile('user1')" style="flex:1; background:#44aaff !important; color:white; font-weight:800;" class="mini-btn">P1 잠금 해제</button>
    <button onclick="unlockProfile('user2')" style="flex:1; background:#44aaff !important; color:white; font-weight:800;" class="mini-btn">P2 잠금 해제</button>
</div>
    
    <label>세션 데이터</label>
    <div style="display:flex;gap:6px;">
        <a href="/export" target="_blank" style="flex:1;">
            <button style="width:100%;background:#444!important;" class="mini-btn">백업 저장</button>
        </a>
        <button onclick="document.getElementById('import-file').click()" style="flex:1;background:#666!important;" class="mini-btn">복원</button>
        <input type="file" id="import-file" style="display:none;" accept=".json" onchange="uploadSessionFile(this)">
    </div>
    
    <label>현재 상황 요약</label>
    <textarea id="m-sum" class="short-textarea" maxlength="500" placeholder="AI가 자동으로 요약하지만, 직접 수정도 가능합니다."></textarea>
    
    <label>AI 엔진 & 모드</label>
    <select id="m-ai-model">
        <option value="gpt-5.2">OpenAI GPT-5.2</option>
        <option value="gpt-4o">OpenAI GPT-4o</option>
        <option value="gemini-3-pro-preview">Google Gemini 3 Pro</option>
    </select>
    <select id="m-solo">
        <option value="false">2인 플레이 모드</option>
        <option value="true">1인 테스트 모드</option>
    </select>
</div>
          </div>
        </div>
        <!-- 서사 -->
        <div id="t-story" class="tab-content">
          <div class="editor-side">
            <label>세션 제목 (최대 30자)</label>
            <input type="text" id="m-title" maxlength="30">
            <label>프롤로그 (최대 1000자)</label>
            <textarea id="m-pro" class="fill-textarea" maxlength="1000" oninput="upCnt(this)"></textarea>
            <div id="cnt-m-pro" class="char-cnt">0/1000</div>
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
        <!-- 키워드 (UI 복구됨) -->
        <div id="t-lore" class="tab-content">
          <div class="editor-side">
             <h3 style="margin:0 0 10px 0; font-size:16px;">키워드/설정 추가</h3>

             <div>
               <label style="font-size:12px; font-weight:bold; color:#888;">키워드 이름 (20자)</label>
               <input type="text" id="kw-t" maxlength="20" placeholder="예: 마법학교, 절대반지">
             </div>

             <div>
               <label style="font-size:12px; font-weight:bold; color:#888;">트리거 (엔터/스페이스로 추가)</label>
               <div id="tag-container" onclick="focusTagInput()">
                 <input type="text" id="tag-input" placeholder="대화에 이 단어가 나오면 AI가 기억합니다">
               </div>
               <input type="hidden" id="tag-hidden" value="">
             </div>

             <div style="flex:1; display:flex; flex-direction:column;">
               <label style="font-size:12px; font-weight:bold; color:#888; margin-bottom:5px;">상세 설정 내용</label>
               <textarea id="kw-c" class="fill-textarea" maxlength="400" oninput="upCnt(this)" placeholder="이 키워드에 대한 설명을 자유롭게 적어주세요."></textarea>
               <div id="cnt-kw-c" class="char-cnt">0/400</div>
             </div>

             <input type="hidden" id="kw-index" value="-1">
             <button onclick="addLoreWithTags()" class="save-btn">저장 / 수정 완료</button>
          </div>

          <div class="list-side">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
              <label style="font-weight:bold;">저장된 키워드</label>
              <span style="font-size:11px; color:#888;">드래그로 우선순위 변경</span>
            </div>
            <div id="lore-list" style="flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:8px;"></div>
          </div>
        </div> <!-- /#t-lore -->
      </div> <!-- /.modal-body -->
    </div>   <!-- /.modal-content -->
  </div>     <!-- /#admin-modal -->

<script>
  const socket = io();
  let gState = null;
  let myRole = null;
  let tags = [];
  let sortable = null;
  let isTypewriter = false;

  // [추가] 현재 수정 중인 메시지의 인덱스 (-1이면 수정 중 아님)
  let editingIdx = -1;

  // [추가] 고유 ID 관리 (브라우저 신분증 - UUID)
  function getClientId(){
    let id = localStorage.getItem('dream_client_id');
    if(!id){
        id = Math.random().toString(36).substring(2) + Date.now().toString(36);
        localStorage.setItem('dream_client_id', id);
    }
    return id;
  }

  function mdToSafeHtml(mdText){
    const raw = marked.parse(mdText || "", {breaks: true});
    return DOMPurify.sanitize(raw, {USE_PROFILES: {html: true}});
  }

  function upCnt(el){
    const id = "cnt-"+el.id;
    const c = document.getElementById(id);
    if(c) c.innerText = el.value.length + "/" + el.getAttribute("maxlength");
  }

  // --- 태그/로어북 관련 함수 (그대로 유지) ---
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
    upCnt(document.getElementById('kw-c'));
  }

  // [수정] 접속 시 Client ID(UUID) 전송 -> 역할 고정용
  socket.on('connect', () => {
    socket.emit('join_game', { client_id: getClientId() });
  });

  socket.on('reload_signal', ()=> window.location.reload());

  socket.on('assign_role', payload=>{
    myRole = payload.role;
    const roleEl = document.getElementById('role-display');
    const badgeEl = document.getElementById('role-badge');

    if(payload.mode === 'readonly'){
      roleEl.innerText = "관전자 모드 (자리가 꽉 찼습니다)";
      if(badgeEl) badgeEl.innerText = "";
      document.getElementById('msg-input').disabled = true;
      document.getElementById('send-btn').disabled = true;
      document.getElementById('skip-btn').disabled = true;
      return;
    }

    const who = (myRole==='user1') ? "Player 1" : "Player 2";
    roleEl.innerText = who + " (당신)";
    if(badgeEl) badgeEl.innerText = (myRole==='user1') ? "(P1)" : "(P2)";
  });

  socket.on('status_update', d=>{
    const s = document.getElementById('status');
    // 준비 완료 상태 메시지는 refreshUI에서 덮어쓰므로,
    // 여기선 일반적인 서버 메시지(AI 집필 중 등)만 처리
    if(gState && gState.session_started){
        s.innerHTML = d.msg;
        s.style.color = d.msg.includes('❌') ? 'red' : 'var(--accent)';
    }
  });

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
        refreshUI();
      }
    }, 20);
  });

  socket.on('typing_update', d => {
    refreshUI(); // 타이핑 상태도 UI 갱신으로 통합 처리
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
  function clearRoles(){
    const pw = prompt("관리자 비밀번호를 입력하세요. 모든 접속 권한이 해제되고 페이지가 새로고침됩니다.");
    if(pw) {
        socket.emit('clear_all_roles', {password: pw});
    }
}
  
  const msgInput = document.getElementById('msg-input');
  msgInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  let typingTimer = null;
  msgInput.addEventListener('input', ()=>{
    if(!myRole || myRole==='readonly') return;
    socket.emit('start_typing', {uid: myRole});
    clearTimeout(typingTimer);
    typingTimer = setTimeout(()=> socket.emit('stop_typing', {uid: myRole}), 1200);
  });
  function unlockProfile(target){
    if(!confirm(target + "의 잠금을 해제하시겠습니까?")) return;
    const pw = prompt("관리자 비밀번호 확인:");
    if(pw) socket.emit('unlock_profile', {password: pw, target: target});
}

  // [핵심] UI 갱신 함수 (모든 상태 반영)
  function refreshUI(){
    if(!gState) return;

    // --- [1] 상태 텍스트 (Status Bar) 로직 수정 ---
    const statusEl = document.getElementById('status');
    const p1 = gState.profiles.user1 || {};
    const p2 = gState.profiles.user2 || {};

    let stHtml = "대기 중...";

    if (!gState.session_started) {
        // 세션 시작 전
        let p1Ready = p1.locked ? "✅" : "⏳";
        let p2Ready = p2.locked ? "✅" : "⏳";
        let p1Name = p1.name || "P1";
        let p2Name = p2.name || "P2";

        if(p1.locked && p2.locked) {
            stHtml = "<span style='color:#00aa00; font-weight:900;'>✨ 모든 플레이어 준비 완료! (마스터가 시작을 눌러주세요)</span>";
        } else {
            stHtml = `${p1Ready} ${p1Name} / ${p2Ready} ${p2Name} (설정 중...)`;
        }
    } else {
        // 세션 시작 후
        const typers = (gState.typing_status||[]);
        const pends = (gState.pending_status||[]);
        const other = (myRole==='user1')?'user2':'user1';
        const otherName = gState.profiles?.[other]?.name || "상대";

        // [핵심 변경] 두 명 다 입력 완료 상태면 AI 집필 중 표시
        if (pends.includes('user1') && pends.includes('user2')) {
            const modelName = gState.ai_model || "AI";
            stHtml = `<span style="color:var(--accent); font-weight:900;">🤔 ${modelName} 집필 중...</span>`;
        }
        else if(typers.includes(other)) {
            stHtml = `<span class="typing-anim">${otherName} 입력 중...</span>`;
        }
        else if(pends.includes(other)) {
            stHtml = `✅ ${otherName} 입력 완료 (당신의 차례)`;
        }
        else {
            stHtml = "행동을 입력하세요.";
        }
        // "나도 완료" 부분 제거함
    }
    statusEl.innerHTML = stHtml;

    // --- [2] 입력창 잠금 로직 ---
    const msg = document.getElementById('msg-input');
    const sendBtn = document.getElementById('send-btn');
    const skipBtn = document.getElementById('skip-btn');

    const pends = (gState.pending_status||[]);
    const myDone = pends.includes(myRole);
    const shouldLock = myDone || !gState.session_started || myRole==='readonly';

    if(msg.disabled !== shouldLock) msg.disabled = shouldLock;
    if(sendBtn.disabled !== shouldLock) sendBtn.disabled = shouldLock;
    if(skipBtn.disabled !== shouldLock) skipBtn.disabled = shouldLock;

    if(!gState.session_started) msg.placeholder = "캐릭터 설정을 완료하고 저장을 눌러주세요.";
    else if(!shouldLock) msg.placeholder = "행동을 입력하세요...";

    // --- [3] 채팅 렌더링 ---
    const cc = document.getElementById('chat-content');

    let html = `<div style="text-align:center;padding:20px;color:var(--accent);font-weight:bold;font-size:1.4em;">${gState.session_title}</div>`;
    html += `<div class="bubble center-ai"><div class="name-tag">PROLOGUE</div>${mdToSafeHtml(gState.prologue||"")}</div>`;

    // 3-1. 확정된 역사(History) 렌더링
    (gState.ai_history||[]).forEach((m, idx) => {
      // 수정 모드
      if(idx === editingIdx) {
        let rawText = m;
        if(rawText.startsWith("**AI**:")) rawText = rawText.replace("**AI**:","").trim();
        html += `
            <div class="bubble center-ai" style="width:90%;">
                <div class="name-tag">EDIT MODE</div>
                <div class="edit-mode-wrap">
                    <textarea id="edit-area-${idx}" class="edit-mode-textarea">${rawText}</textarea>
                    <div class="edit-actions">
                        <button class="mini-btn" style="background:#888" onclick="cancelEdit()">취소</button>
                        <button class="mini-btn" style="background:var(--accent);color:#fff" onclick="saveEdit(${idx})">저장</button>
                    </div>
                </div>
            </div>`;
        return;
      }

      // 일반 모드
      let content = "";
      let nameHtml = "";
      let alignClass = "align-left";

      if(m.startsWith("**AI**:")){
        const text = m.replace("**AI**:","").trim();
        alignClass = "center-ai";
        nameHtml = `<div class="name-tag">AI <button class="edit-btn" onclick="startEdit(${idx})">수정</button></div>`;
        content = mdToSafeHtml(text);
      } else if(m.startsWith("**Round**:")){
        const raw = m.replace("**Round**:", "").trim();
        const parts = raw.split(" / ");
        let roundHtml = "";
        parts.forEach(p => {
            const sep = p.indexOf(":");
            if(sep > -1){
                const name = p.substring(0, sep).trim();
                const body = p.substring(sep+1).trim();
                const myProfileName = gState.profiles[myRole]?.name;
                const isMe = (name === myProfileName);
                const subAlign = isMe ? "align-right" : "align-left";
                roundHtml += `<div class="bubble ${subAlign}"><div class="name-tag">${name}</div>${mdToSafeHtml(body)}</div>`;
            } else {
                roundHtml += `<div class="bubble align-left">${mdToSafeHtml(p)}</div>`;
            }
        });
        html += roundHtml;
        return;
      } else {
        content = mdToSafeHtml(m);
      }

      if(!m.startsWith("**Round**:")){
         html += `<div class="bubble ${alignClass}">${nameHtml}${content}</div>`;
      }
    });

    // 3-2. [핵심 변경] 실시간 입력 메시지 (시간순 정렬)
    // 딕셔너리를 배열로 변환
    let pendingMsgs = [];
    if(gState.pending_inputs){
        Object.keys(gState.pending_inputs).forEach(uid => {
            const item = gState.pending_inputs[uid];
            // 텍스트가 있는 경우만
            if(item && item.text){
                pendingMsgs.push({
                    uid: uid,
                    text: item.text,
                    ts: item.ts || "" // 타임스탬프
                });
            }
        });
    }

    // 타임스탬프 기준 오름차순 정렬 (먼저 친 게 위로)
    pendingMsgs.sort((a, b) => {
        if (a.ts < b.ts) return -1;
        if (a.ts > b.ts) return 1;
        return 0;
    });

    // 정렬된 순서대로 렌더링
    pendingMsgs.forEach(msg => {
        const uName = gState.profiles[msg.uid].name;
        const isMe = (msg.uid === myRole);
        const align = isMe ? "align-right" : "align-left";

        html += `<div class="bubble ${align}"><div class="name-tag">${uName}</div>${mdToSafeHtml(msg.text)}</div>`;
    });

    if(cc.innerHTML !== html) {
        cc.innerHTML = html;
        if(editingIdx === -1) {
            document.getElementById('chat-window').scrollTop = document.getElementById('chat-window').scrollHeight;
        }
    }

    // --- [4] 프로필 복원 ---
    const p = (myRole && gState.profiles && gState.profiles[myRole]) ? gState.profiles[myRole] : {name:"",bio:"",canon:"",locked:false};
    const activeId = document.activeElement?.id || "";

    if(activeId!=='p-name' && (!document.getElementById('p-name').value || p.locked))
        document.getElementById('p-name').value = p.name || "";

    if(p.locked || (activeId!=='p-bio' && !document.getElementById('p-bio').value)) {
         document.getElementById('p-bio').value = p.bio || "";
         upCnt(document.getElementById('p-bio'));
    }
    if(p.locked || (activeId!=='p-canon' && !document.getElementById('p-canon').value)) {
         document.getElementById('p-canon').value = p.canon || "";
         upCnt(document.getElementById('p-canon'));
    }

    const locked = !!p.locked;
    const overlay = document.getElementById('profile-lock-overlay');
    if(overlay) overlay.style.display = locked ? 'block' : 'none';
    const disableProfile = (myRole==='readonly') || locked;
    document.getElementById('p-name').readOnly = disableProfile;
    document.getElementById('p-name').disabled = disableProfile;
    document.getElementById('p-bio').disabled = disableProfile;
    document.getElementById('p-canon').disabled = disableProfile;

    const rb = document.getElementById('ready-btn');
    rb.disabled = disableProfile;
    rb.innerText = locked ? "설정이 확정되었습니다" : "설정 저장";

    const roleEl = document.getElementById('role-display');
    const badgeEl = document.getElementById('role-badge');

    if(myRole && myRole !== 'readonly'){
        let who = (myRole==='user1') ? "Player 1" : "Player 2";
        roleEl.innerText = who + " (당신)";
        if(badgeEl) badgeEl.innerText = (myRole==='user1') ? "(P1)" : "(P2)";
    }

    // --- [5] 마스터 데이터 복원 ---
    if(activeId!=='m-title') document.getElementById('m-title').value = gState.session_title || "";
    if(activeId!=='m-sys') { document.getElementById('m-sys').value = gState.sys_prompt || ""; upCnt(document.getElementById('m-sys')); }
    if(activeId!=='m-pro') { document.getElementById('m-pro').value = gState.prologue || ""; upCnt(document.getElementById('m-pro')); }
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
    if(activeId!=='kw-c') upCnt(document.getElementById('kw-c'));
    renderLoreList();
  }


  // [추가] 수정 모드 제어 함수들
  function startEdit(idx){
    editingIdx = idx;
    refreshUI();
  }
  function cancelEdit(){
    editingIdx = -1;
    refreshUI();
  }
  function saveEdit(idx){
    const txt = document.getElementById(`edit-area-${idx}`).value;
    // AI 접두사 다시 붙여서 전송
    socket.emit('edit_history_msg', {index: idx, text: "**AI**: " + txt});
    editingIdx = -1;
  }

  function send(){
    const t = document.getElementById('msg-input').value.trim();
    if(!t) return;
    document.getElementById('send-btn').disabled = true;
    document.getElementById('msg-input').disabled = true;
    socket.emit('client_message', {uid: myRole, text: t});
    document.getElementById('msg-input').value='';
    socket.emit('stop_typing', {uid: myRole});
  }

  function skipTurn(){
    if(!confirm("스킵?")) return;
    socket.emit('skip_turn', {uid: myRole});
    socket.emit('stop_typing', {uid: myRole});
  }

  function saveProfile(){
    const name = document.getElementById('p-name').value;
    if(!name) return alert("이름 필수");
    if(confirm("확정하시겠습니까? (수정 불가)")){
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
    alert("저장됨");
  }

  function startSession(){ socket.emit('start_session'); }
  function sessionReset(){
    if(!confirm("정말로 초기화하시겠습니까?")) return;
    const pw = prompt("관리자 비밀번호를 입력하세요:");
    if(!pw) return;
    socket.emit('reset_session', {password: pw});
    try{
      document.getElementById('chat-content').innerHTML = "";
      document.getElementById('msg-input').value = "";
      clearLoreEditor();
    } catch(e){}
    const modal = document.getElementById('admin-modal');
    if(modal) modal.style.display = 'none';
  }

  function saveExamples(){
    const exs = [];
    for(let i=0;i<3;i++){
      exs.push({ q: document.getElementById(`ex-q-${i}`).value, a: document.getElementById(`ex-a-${i}`).value });
    }
    socket.emit('save_examples', exs);
    alert("저장됨");
  }

  function addLoreWithTags(){
    const title = document.getElementById('kw-t').value;
    const content = document.getElementById('kw-c').value;
    const triggers = document.getElementById('tag-hidden').value;
    const idx = parseInt(document.getElementById('kw-index').value);
    if(!title) return alert("이름 필요");
    if(!triggers) return alert("트리거 필요");
    socket.emit('add_lore', {title, triggers, content, index: idx});
    clearLoreEditor();
  }

  function editLore(i){
    const l = gState.lorebook[i];
    document.getElementById('kw-t').value = l.title || "";
    document.getElementById('kw-c').value = l.content || "";
    document.getElementById('kw-index').value = i;
    loadTagsFromString(l.triggers || "");
    upCnt(document.getElementById('kw-c'));
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
        if(evt.oldIndex === evt.newIndex) socket.emit('reorder_lore', {from: evt.oldIndex, to: evt.newIndex});
      }
    });
  }

  function uploadSessionFile(input){
    if(!input.files || !input.files[0]) return;
    const formData = new FormData();
    formData.append('file', input.files[0]);
    fetch('/import',{method:'POST',body:formData})
      .then(res=>{ if(res.ok) alert("복원됨"); else alert("실패"); input.value=''; })
      .catch(err=>alert("오류: "+err));
  }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # 1. Pinggy 터널링 함수 (백그라운드 실행)
    def start_pinggy():
        print("🚀 [드림놀이] Pinggy 서버 연결 중...")
        # Pinggy에 SSH로 포트 포워딩 연결 (엄격한 호스트 키 검사 비활성화)
        cmd = "ssh -o StrictHostKeyChecking=no -p 443 -R0:localhost:5000 a.pinggy.io"

        # 프로세스 실행
        process = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 출력되는 로그에서 URL 찾기
        print("\n" + "="*50)
        print("🔗 아래 주소로 접속하세요 (잠시 후 뜹니다):")
        try:
            while True:
                line = process.stdout.readline()
                if not line: break
                # Pinggy는 주소를 텍스트로 뱉어줌
                if "http" in line:
                    print(f"\n👉 {line.strip()}\n")
                    print("="*50 + "\n")
        except Exception as e:
            print(f"Pinggy Error: {e}")

    # 2. 별도 스레드에서 Pinggy 실행
    t = threading.Thread(target=start_pinggy)
    t.daemon = True
    t.start()

    # 3. Flask-SocketIO 서버 실행
    # 잠시 대기 후 실행하여 로그 겹침 방지
    time.sleep(3)
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
