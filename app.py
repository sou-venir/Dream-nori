# app.py (깃허브/로컬 배포용 완전체)
import os, json, copy, re
from datetime import datetime
from flask import Flask, render_template_string, request, Response
from flask_socketio import SocketIO, emit
from pyngrok import ngrok
import openai
import google.generativeai as genai
from dotenv import load_dotenv

# 1. 환경변수 로드 (.env 파일이 있으면 읽음)
load_dotenv()

# 2. 설정 및 키 가져오기
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "save_data.json")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "3896")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
NGROK_AUTH_TOKEN = os.getenv("NGROK_AUTH_TOKEN", "").strip()

# 3. AI 클라이언트 설정
if not OPENAI_API_KEY:
    print("⚠️ 경고: OPENAI_API_KEY가 없습니다. AI가 작동하지 않을 수 있습니다.")

client = openai.OpenAI(api_key=OPENAI_API_KEY)

gemini_model = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-3-pro-preview')
    except:
        pass

if NGROK_AUTH_TOKEN:
    ngrok.set_auth_token(NGROK_AUTH_TOKEN)

# 4. 앱 초기화
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins="*")

# 5. 상태(State) 초기화
initial_state = {
    "session_title": "드림놀이",
    "theme": {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"},
    "ai_model": "gpt-5.2", 
    "admin_password": ADMIN_PASSWORD_ENV,
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

# 6. 저장소 로직
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

saved_state = load_data()
state = saved_state if isinstance(saved_state, dict) else copy.deepcopy(initial_state)
# 환경변수 비번 강제 적용
if ADMIN_PASSWORD: state["admin_password"] = ADMIN_PASSWORD

connected_users = {"user1": None, "user2": None}
typing_users = set()
readonly_sids = set()
admin_sids = set()

# 7. 헬퍼 함수들
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
    if connected_users["user1"]: socketio.emit("initial_state", payload, room=connected_users["user1"])
    if connected_users["user2"]: socketio.emit("initial_state", payload, room=connected_users["user2"])

def analyze_theme_color(title, sys_prompt):
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"JSON output only: {\"bg\":\"#Hex\",\"panel\":\"#Hex\",\"accent\":\"#Hex\"}"},
                {"role":"user","content":f"Title: {title}\nPrompt: {sys_prompt[:800]}"}
            ],
            response_format={"type":"json_object"}
        )
        return json.loads(res.choices[0].message.content)
    except: return state.get("theme")

MAX_CONTEXT_CHARS_BUDGET = 14000
HISTORY_SOFT_LIMIT_CHARS = 9500
SUMMARY_MAX_CHARS = 500
TARGET_MAX_TOKENS = 1100 

def build_history_block():
    history = state.get("ai_history", [])
    collected, total = [], 0
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
    rough = len(sys_p) + len(pro) + len(summ) + len(hist) + len(extra_incoming) + 2000
    return rough > MAX_CONTEXT_CHARS_BUDGET

def auto_summary_apply():
    def run_once():
        recent_log = "\n".join(state.get("ai_history", [])[-60:])
        if not recent_log: return None
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":f"Summarize in 2-3 sentences:\n{recent_log}"}]
        )
        return (res.choices[0].message.content or "").strip()
    try:
        s = run_once()
        if s: state["summary"] = s[:SUMMARY_MAX_CHARS]; save_data()
    except: pass

# =========================
# 8. Routes & Socket
# =========================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, theme=state.get("theme"))

@app.route("/export")
def export_config():
    # 설정만 내보내기
    cfg = {k: state.get(k) for k in ["session_title","sys_prompt","prologue","ai_model","examples","lorebook","solo_mode"]}
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    fname = f"session_{ts}.json"
    return Response(json.dumps(cfg, ensure_ascii=False, indent=2), mimetype="application/json", headers={"Content-Disposition": f"attachment;filename={fname}"})

@app.route("/import", methods=["POST"])
def import_config():
    try:
        data = json.load(request.files["file"])
        for k in ["session_title","sys_prompt","prologue","ai_model","examples","lorebook","solo_mode"]:
            if k in data: state[k] = copy.deepcopy(data[k])
        save_data(); emit_state_to_players(); socketio.emit("reload_signal")
        return "OK", 200
    except: return "Error", 500

@socketio.on("join_game")
def join_game(data=None):
    sid = request.sid
    saved_role = data.get("saved_role") if data else None
    
    # 재접속 처리
    if saved_role in connected_users and connected_users[saved_role] is None:
        connected_users[saved_role] = sid
        emit("assign_role", {"role": saved_role, "mode": "player"})
        emit_state_to_players()
        return

    for role, rsid in connected_users.items():
        if rsid == sid: emit("assign_role", {"role": role, "mode": "player"}); emit_state_to_players(); return

    if connected_users["user1"] is None:
        connected_users["user1"] = sid
        emit("assign_role", {"role": "user1", "mode": "player"})
    elif connected_users["user2"] is None:
        connected_users["user2"] = sid
        emit("assign_role", {"role": "user2", "mode": "player"})
    else:
        readonly_sids.add(sid)
        emit("assign_role", {"role": "readonly", "mode": "readonly"})
    emit_state_to_players()

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
    save_data(); emit_state_to_players()

@socketio.on("start_typing")
def start_typing(data):
    uid = data.get("uid")
    if uid in ("user1","user2") and connected_users.get(uid) == request.sid:
        typing_users.add(uid); emit_state_to_players()

@socketio.on("stop_typing")
def stop_typing(data):
    uid = data.get("uid")
    typing_users.discard(uid); emit_state_to_players()

@socketio.on("check_admin")
def check_admin(data):
    if str(data.get("password")) == str(state.get("admin_password")):
        admin_sids.add(request.sid); emit("admin_auth_res", {"success": True})
    else: emit("admin_auth_res", {"success": False})

@socketio.on("save_master_base")
def save_master_base(data):
    state.update({k: data[k] for k in ["title", "sys", "pro", "sum", "model", "solo_mode"] if k in data})
    # 키 매핑 수정 (프론트에서 title -> state session_title)
    if "title" in data: state["session_title"] = data["title"]
    if "sys" in data: state["sys_prompt"] = data["sys"]
    if "pro" in data: state["prologue"] = data["pro"]
    if "sum" in data: state["summary"] = data["sum"]
    if "model" in data: state["ai_model"] = data["model"]
    if "solo_mode" in data: state["solo_mode"] = data["solo_mode"]
    
    save_data(); emit_state_to_players()

@socketio.on("theme_analyze_request")
def theme_analyze_request(_=None):
    if state["sys_prompt"]:
        state["theme"] = analyze_theme_color(state["session_title"], state["sys_prompt"])
        save_data(); emit_state_to_players(); socketio.emit("reload_signal")

@socketio.on("save_examples")
def save_examples(data):
    state["examples"] = [{"q":d.get("q",""), "a":d.get("a","")} for d in data]
    save_data(); emit_state_to_players()

@socketio.on("update_profile")
def update_profile(data):
    uid = data.get("uid")
    if uid in state["profiles"]:
        state["profiles"][uid].update({
            "name": data.get("name")[:12],
            "bio": data.get("bio")[:200],
            "canon": data.get("canon")[:350],
            "locked": True
        })
        save_data(); emit_state_to_players()

@socketio.on("start_session")
def start_session(_=None):
    if request.sid in admin_sids:
        state["session_started"] = True; save_data(); emit_state_to_players()
        emit("status_update", {"msg": "✅ 세션이 시작되었습니다!"}, broadcast=True)

@socketio.on("add_lore")
def add_lore(data):
    item = {"title":data.get("title")[:10], "triggers":data.get("triggers"), "content":data.get("content")[:400]}
    idx = int(data.get("index", -1))
    if 0 <= idx < len(state["lorebook"]): state["lorebook"][idx] = item
    else: state["lorebook"].append(item)
    save_data(); emit_state_to_players()

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

@socketio.on("reset_session")
def reset_session(data):
    if str(data.get("password")) == str(state.get("admin_password")):
        state["ai_history"] = []
        state["lorebook"] = []
        state["summary"] = ""
        state["pending_inputs"] = {}
        typing_users.clear()
        state["session_started"] = False
        state["profiles"]["user1"]["locked"] = False
        state["profiles"]["user2"]["locked"] = False
        save_data(); emit_state_to_players()

# 9. 합작 채팅 로직
def record_pending(uid, text):
    state.setdefault("pending_inputs", {})
    state["pending_inputs"][uid] = {"text": text[:600], "ts": datetime.now().isoformat()}
    save_data()

def both_ready():
    if state.get("solo_mode"): return "user1" in state.get("pending_inputs", {})
    return "user1" in state.get("pending_inputs", {}) and "user2" in state.get("pending_inputs", {})

def trigger_ai():
    try:
        p = state.get("pending_inputs", {})
        p1t, p2t = p.get("user1", {}).get("text","(스킵)"), p.get("user2", {}).get("text","(스킵)")
        p1n, p2n = state["profiles"]["user1"]["name"], state["profiles"]["user2"]["name"]
        
        merged = f"{p1t}\n{p2t}"
        active = []
        for l in state.get("lorebook", []):
            if any(t.strip() in merged for t in l["triggers"].split(",")):
                active.append(f"[{l['title']}]: {l['content']}")
        
        sys = f"{state['sys_prompt']}\n\n[요약]\n{state['summary']}\n\n[키워드]\n" + "\n".join(active[:3])
        if would_overflow_context(sys + merged):
            auto_summary_apply()
            sys = f"{state['sys_prompt']}\n\n[요약]\n{state['summary']}\n\n[키워드]\n" + "\n".join(active[:3])
            
        round_block = f"--- [ROUND] ---\n<{p1n}>: {p1t}\n<{p2n}>: {p2t}\n--- [CMD] ---\n위 두 행동은 동시간대. 통합하여 2000자 내외 서사 작성."
        
        msgs = [{"role":"system","content":sys}]
        for ex in state.get("examples", []):
            if ex["q"]: msgs.extend([{"role":"user","content":ex["q"]}, {"role":"assistant","content":ex["a"]}])
        for h in build_history_block():
            msgs.append({"role": "assistant" if h.startswith("**AI**") else "user", "content": h})
        msgs.append({"role":"user","content": round_block})

        model = state.get("ai_model", "gpt-5.2")
        socketio.emit("status_update", {"msg": f"✍️ {model} 집필 중..."})
        
        ai_res = ""
        if "gemini" in model.lower() and gemini_model:
            from google.generativeai.types import HarmCategory, HarmBlockThreshold
            safe = {HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE}
            ai_res = gemini_model.generate_content(sys+"\n"+round_block, safety_settings=safe).text
        else:
            res = client.chat.completions.create(model=model, messages=msgs, max_tokens=TARGET_MAX_TOKENS)
            ai_res = res.choices[0].message.content

        state["ai_history"].append(f"**Round**: {p1n}:{p1t} / {p2n}:{p2t}")
        state["ai_history"].append(f"**AI**: {ai_res}")
        state["pending_inputs"] = {}
        save_data()
        
        socketio.emit("ai_typewriter_event", {"content": ai_res})
        emit_state_to_players()
    except Exception as e:
        socketio.emit("status_update", {"msg": f"Error: {e}"})

@socketio.on("client_message")
def client_message(data):
    uid, text = data.get("uid"), (data.get("text") or "").strip()
    if uid not in ("user1","user2") or not state.get("session_started"): return
    record_pending(uid, text)
    typing_users.discard(uid)
    emit_state_to_players()
    
    if both_ready(): trigger_ai()
    else:
        other = "user2" if uid=="user1" else "user1"
        nm = state["profiles"][other]["name"]
        socketio.emit("status_update", {"msg": f"⏳ {nm}님 입력 대기... (스킵 가능)"})

@socketio.on("skip_turn")
def skip_turn(data):
    uid = data.get("uid")
    if uid not in ("user1","user2") or not state.get("session_started"): return
    record_pending(uid, "(스킵)")
    typing_users.discard(uid)
    emit_state_to_players()
    if both_ready(): trigger_ai()
    else:
        other = "user2" if uid=="user1" else "user1"
        nm = state["profiles"][other]["name"]
        socketio.emit("status_update", {"msg": f"⏳ {nm}님 입력 대기... (스킵 가능)"})

# 10. HTML Template (UI Fixed)
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
    :root{ --bg:{{theme.bg}}; --panel:{{theme.panel}}; --accent:{{theme.accent}}; }
    html,body{height:100%;margin:0;overflow:hidden;font-family:Pretendard,sans-serif;background:var(--bg);color:#000;}
    #main{flex:1;display:flex;flex-direction:column;height:100vh;min-width:0;}
    #chat-window{flex:1;overflow-y:auto;padding:30px 10%;display:flex;flex-direction:column;gap:15px;scroll-behavior:smooth;}
    #chat-content{display:flex;flex-direction:column;gap:15px;}
    #sidebar{width:320px;height:100vh;background:var(--panel);display:flex;flex-direction:column;overflow:hidden;}
    #sidebar-body{padding:20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;}
    #sidebar-footer{padding:12px 20px 16px;border-top:1px solid rgba(0,0,0,0.06);background:var(--panel);}
    textarea,input,select{background:var(--bg)!important;border:1px solid rgba(0,0,0,0.1)!important;border-radius:10px;padding:10px;width:100%;box-sizing:border-box;resize:none!important;color:#000!important;}
    #msg-input{background:var(--panel)!important;height:80px;}
    button{cursor:pointer;border:none;border-radius:8px;background:var(--accent);padding:10px;font-weight:bold;color:#fff;}
    .master-btn{width:100%;background:transparent!important;color:#000!important;border:1px solid #ddd!important;font-weight:800;}
    .bubble{padding:15px 20px;border-radius:15px;max-width:85%;line-height:1.6;font-size:14px;white-space:pre-wrap;background:rgba(0,0,0,0.03);}
    .center-ai{align-self:center;background:var(--panel)!important;border-left:5px solid var(--accent);width:100%;max-width:800px;box-shadow:0 4px 15px rgba(0,0,0,0.05);}
    .user-bubble{align-self:center;background:#eee;color:#000;font-size:12px;padding:6px 12px;border-radius:20px;max-width:80%;}
    #admin-modal{display:none;position:fixed;z-index:10000;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(5px);align-items:center;justify-content:center;padding:24px;}
    .modal-content{width:100%;max-width:1200px;height:min(85vh,900px);background:#fff;border-radius:16px;display:flex;flex-direction:column;overflow:hidden;}
    .modal-header{height:60px;display:flex;justify-content:space-between;align-items:center;padding:0 20px;background:#f8f9fa;border-bottom:1px solid #eee;}
    .tab-btn{border:none;background:none;padding:0 14px;height:100%;font-size:14px;font-weight:700;color:#000;cursor:pointer;opacity:0.6;}
    .tab-btn.active{color:var(--accent);border-bottom:3px solid var(--accent);opacity:1;}
    .modal-body{flex:1;display:flex;overflow:hidden;}
    .tab-content{display:none;width:100%;height:100%;flex-direction:row;}
    .tab-content.active{display:flex;}
    .editor-side{flex:1.25;padding:20px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;border-right:1px solid #f0f0f0;}
    .list-side{flex:.75;padding:20px;background:#fafafa;display:flex;flex-direction:column;gap:12px;overflow-y:auto;}
    .save-btn{background:var(--accent);color:#fff;padding:14px;border-radius:10px;font-weight:800;border:none;}
    .fill-textarea{flex:1;min-height:260px;}
    .short-textarea{flex:none;height:120px;}
    .ex-block{background:#fff;border:1px solid #eee;padding:12px;border-radius:10px;display:flex;flex-direction:column;gap:8px;}
    .ex-block textarea{height:130px!important;}
    textarea::placeholder{color:rgba(0,0,0,0.4);font-weight:700;}
    .lore-row{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:10px;background:rgba(0,0,0,0.03);border:1px solid #eee;}
    .mini-btn{padding:3px 7px;font-size:11px;border-radius:8px;background:#666;color:#fff;}
    .typing-anim{animation:blink 1.4s infinite;} @keyframes blink{50%{opacity:0.45;}}
    body,h1,h2,h3,p,span,div,label,input,textarea,select{color:#000!important;}
  </style>
</head>
<body>
  <div id="main">
    <div id="chat-window"><div id="chat-content"></div></div>
    <div id="input-area" style="padding:20px;background:var(--bg);">
      <div id="status" style="font-size:12px;margin-bottom:5px;font-weight:bold;">대기 중</div>
      <div style="display:flex;gap:10px;align-items:stretch;">
        <textarea id="msg-input" maxlength="600" placeholder="행동을 입력하세요..."></textarea>
        <div style="display:flex;flex-direction:column;gap:8px;width:110px;">
          <button id="send-btn" onclick="send()" style="width:110px;">전송</button>
          <button id="skip-btn" onclick="skipTurn()" style="width:110px;background:transparent;border:1px solid #ddd;color:#666;font-size:12px;">스킵</button>
        </div>
      </div>
    </div>
  </div>
  <div id="sidebar">
    <div id="sidebar-body">
      <h3>설정 <span id="role-badge" style="font-size:12px;color:var(--accent)"></span></h3>
      <div id="role-display" style="padding:10px;background:rgba(0,0,0,0.05);border-radius:8px;font-weight:800;margin-bottom:10px;">접속 중...</div>
      <input type="text" id="p-name" maxlength="12" placeholder="이름">
      <textarea id="p-bio" maxlength="200" style="height:120px;" placeholder="캐릭터 설정"></textarea>
      <textarea id="p-canon" maxlength="350" style="height:80px;" placeholder="관계 설정"></textarea>
      <button onclick="saveProfile()" id="ready-btn">설정 저장</button>
    </div>
    <div id="sidebar-footer"><button class="master-btn" onclick="requestAdmin()">마스터 설정</button></div>
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
        <button onclick="closeModal(true)" style="background:none;font-size:20px;">✕</button>
      </div>
      <div class="modal-body">
        <div id="t-base" class="tab-content active">
          <div class="editor-side" style="display:flex;flex-direction:column;min-height:0;">
            <label>시스템 프롬프트</label><textarea id="m-sys" class="fill-textarea" style="flex:1;min-height:0;"></textarea>
            <button onclick="saveMaster()" class="save-btn" style="flex:0 0 auto;">저장</button>
          </div>
          <div class="list-side" style="display:flex;flex-direction:column;min-height:0;">
            <label>세션 설정</label>
            <div style="display:flex;gap:6px;">
              <button onclick="window.open('/export')" class="mini-btn" style="flex:1;">백업 저장</button>
              <button onclick="document.getElementById('imp-f').click()" class="mini-btn" style="flex:1;">복원</button>
              <input type="file" id="imp-f" style="display:none;" onchange="uploadFile(this)">
            </div>
            <textarea id="m-sum" class="short-textarea" placeholder="상황 요약"></textarea>
            <label>AI 모델</label>
            <select id="m-ai-model"><option value="gpt-5.2">GPT-5.2</option><option value="gpt-4o">GPT-4o</option><option value="gemini-3-pro-preview">Gemini 3</option></select>
            <label>1인 모드</label><select id="m-solo"><option value="false">OFF</option><option value="true">ON</option></select>
            <div style="margin-top:auto;display:flex;gap:8px;">
              <button id="start-session-btn" onclick="startSession()" class="save-btn" style="display:none;flex:1;background:#444!important;">세션 시작</button>
              <button id="reset-session-btn" onclick="sessionReset()" class="mini-btn" style="display:none;flex:1;background:#f44!important;height:100%;">초기화</button>
            </div>
          </div>
        </div>
        <div id="t-story" class="tab-content">
          <div class="editor-side"><label>제목</label><input id="m-title"><label>프롤로그</label><textarea id="m-pro" class="fill-textarea"></textarea><button onclick="saveMaster()" class="save-btn">저장</button></div>
          <div class="list-side"><label>안내</label><p style="font-size:13px;color:#666;">프롬프트와 프롤로그 입력 시 테마가 자동 분석됩니다.</p></div>
        </div>
        <div id="t-ex" class="tab-content">
          <div class="editor-side">
             <div class="ex-block"><label>Ex 1</label><textarea id="ex-q-0" placeholder="질문"></textarea><textarea id="ex-a-0" placeholder="답변"></textarea></div>
             <div class="ex-block"><label>Ex 2</label><textarea id="ex-q-1" placeholder="질문"></textarea><textarea id="ex-a-1" placeholder="답변"></textarea></div>
             <div class="ex-block"><label>Ex 3</label><textarea id="ex-q-2" placeholder="질문"></textarea><textarea id="ex-a-2" placeholder="답변"></textarea></div>
             <button onclick="saveExamples()" class="save-btn">저장</button>
          </div>
        </div>
        <div id="t-lore" class="tab-content">
          <div class="editor-side" style="display:flex;flex-direction:column;min-height:0;">
             <label>키워드</label><input id="kw-t" placeholder="이름"><input id="kw-tr" placeholder="트리거"><textarea id="kw-c" class="fill-textarea" style="flex:1;min-height:0;"></textarea>
             <input type="hidden" id="kw-index" value="-1"><button onclick="addLoreWithTags()" class="save-btn" style="flex:0 0 auto;">저장/수정</button>
          </div>
          <div class="list-side" id="lore-list"></div>
        </div>
      </div>
    </div>
  </div>
<script>
  const socket = io(); let gState = null; let myRole = null; let isTypingEffect = false;
  function mdToSafeHtml(md){ return DOMPurify.sanitize(marked.parse(md || ""), {USE_PROFILES:{html:true}}); }
  socket.on('connect', () => { socket.emit('join_game', {saved_role: localStorage.getItem('dream_role')}); });
  socket.on('assign_role', d => {
    myRole = d.role; if(myRole && myRole!=='readonly') localStorage.setItem('dream_role', myRole);
    document.getElementById('role-display').innerText = (myRole==='user1')?"Player 1 (당신)":(myRole==='user2')?"Player 2 (당신)":"관전 모드";
    document.getElementById('role-badge').innerText = (myRole==='user1')?"(P1)":(myRole==='user2')?"(P2)":"";
    if(myRole==='readonly'){ document.getElementById('msg-input').disabled=true; document.getElementById('send-btn').disabled=true; document.getElementById('skip-btn').disabled=true; }
  });
  socket.on('reload_signal', () => location.reload());
  socket.on('status_update', d => { document.getElementById('status').innerHTML = d.msg; });
  socket.on('ai_typewriter_event', d => {
    isTypewriter = true; const cc = document.getElementById('chat-content');
    const wrap = document.createElement('div'); wrap.className='bubble center-ai'; wrap.innerHTML='<div class="name-tag">AI</div>'; cc.appendChild(wrap);
    let i=0; const full=d.content; const tick=setInterval(()=>{
      i+=5; if(i>full.length) i=full.length;
      wrap.innerHTML='<div class="name-tag">AI</div>'+mdToSafeHtml(full.slice(0,i));
      document.getElementById('chat-window').scrollTop = document.getElementById('chat-window').scrollHeight;
      if(i>=full.length){ clearInterval(tick); isTypewriter=false; }
    },20);
  });
  socket.on('admin_auth_res', d => {
    if(d.success){
      document.getElementById('admin-modal').style.display='flex';
      document.getElementById('start-session-btn').style.display='block';
      document.getElementById('reset-session-btn').style.display='block';
      refreshUI();
    } else alert("비번 틀림");
  });
  socket.on('initial_state', d => {
    gState = d;
    if(d.theme){ document.documentElement.style.setProperty('--bg', d.theme.bg); document.documentElement.style.setProperty('--panel', d.theme.panel); document.documentElement.style.setProperty('--accent', d.theme.accent); }
    if(!isTypewriter) refreshUI();
  });
  function requestAdmin(){ const pw=prompt("PW:"); if(pw) socket.emit('check_admin',{password:pw}); }
  function closeModal(a){ document.getElementById('admin-modal').style.display='none'; if(a) socket.emit('theme_analyze_request'); }
  function openTab(e,id){
    document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.getElementById(id).classList.add('active'); e.currentTarget.classList.add('active');
  }
  const msgInput=document.getElementById('msg-input'); let tTimer=null;
  msgInput.addEventListener('input',()=>{ if(!myRole || myRole==='readonly') return; socket.emit('start_typing',{uid:myRole}); clearTimeout(tTimer); tTimer=setTimeout(()=>socket.emit('stop_typing',{uid:myRole}),1200); });
  function refreshUI(){
    if(!gState) return;
    // Status
    const pends=gState.pending_status||[], typers=gState.typing_status||[], other=(myRole==='user1')?'user2':'user1';
    let st="대기 중..."; if(typers.includes(other)) st=`<span class="typing-anim">상대 입력 중...</span>`; else if(pends.includes(other)) st=`✅ 상대 입력 완료`;
    if(pends.includes(myRole)) st+=" / 나도 완료"; document.getElementById('status').innerHTML=st;
    // Buttons
    const myDone = pends.includes(myRole);
    const locked = myDone || !gState.session_started || myRole==='readonly';
    document.getElementById('send-btn').disabled = locked;
    document.getElementById('skip-btn').disabled = locked;
    document.getElementById('msg-input').disabled = locked; 
    if(gState.session_started && !locked) document.getElementById('msg-input').placeholder="행동을 입력하세요...";
    // Chat
    const cc=document.getElementById('chat-content'); let h=`<div style="text-align:center;padding:20px;font-weight:bold;font-size:1.4em;">${gState.session_title}</div>`;
    h+=`<div class="bubble center-ai"><div class="name-tag">PROLOGUE</div>${mdToSafeHtml(gState.prologue)}</div>`;
    gState.ai_history.forEach(m=>{
      if(m.startsWith("**AI**:")) h+=`<div class="bubble center-ai"><div class="name-tag">AI</div>${mdToSafeHtml(m.replace("**AI**:",""))}</div>`;
      else h+=`<div class="user-bubble">${mdToSafeHtml(m.replace("**Round**:",""))}</div>`;
    });
    cc.innerHTML=h;
    // Forms
    const p=gState.profiles[myRole]||{}; document.getElementById('p-name').value=p.name||""; document.getElementById('p-bio').value=p.bio||""; document.getElementById('p-canon').value=p.canon||"";
    if(p.locked) document.getElementById('ready-btn').disabled=true;
    // Admin Restore
    document.getElementById('m-title').value=gState.session_title; document.getElementById('m-sys').value=gState.sys_prompt; document.getElementById('m-pro').value=gState.prologue;
    document.getElementById('m-sum').value=gState.summary; document.getElementById('m-ai-model').value=gState.ai_model; document.getElementById('m-solo').value=gState.solo_mode?"true":"false";
    gState.examples.forEach((ex,i)=>{ if(document.getElementById(`ex-q-${i}`)) { document.getElementById(`ex-q-${i}`).value=ex.q; document.getElementById(`ex-a-${i}`).value=ex.a; }});
    // Lore
    const list=document.getElementById('lore-list');
    list.innerHTML=gState.lorebook.map((l,i)=>`<div class="lore-row" data-index="${i}"><div class="drag-handle" style="cursor:grab;margin-right:10px;">☰</div><div style="flex:1"><b>${l.title}</b> (${l.triggers})</div><button class="mini-btn" onclick="editLore(${i})">수정</button><button class="mini-btn" style="background:#f44" onclick="delLore(${i})">삭제</button></div>`).join('');
    if(sortable) sortable.destroy(); sortable=new Sortable(list,{handle:'.drag-handle',animation:120,onEnd:(evt)=>{if(evt.oldIndex!==evt.newIndex) socket.emit('reorder_lore',{from:evt.oldIndex,to:evt.newIndex});}});
  }
  function send(){
    const t=document.getElementById('msg-input').value.trim(); if(!t) return;
    document.getElementById('msg-input').disabled=true; document.getElementById('send-btn').disabled=true;
    socket.emit('client_message',{uid:myRole,text:t}); document.getElementById('msg-input').value=''; socket.emit('stop_typing',{uid:myRole});
  }
  function skipTurn(){ if(confirm("스킵?")) { socket.emit('skip_turn',{uid:myRole}); socket.emit('stop_typing',{uid:myRole}); } }
  function saveProfile(){ const n=document.getElementById('p-name').value; if(n && confirm("확정?")) socket.emit('update_profile',{uid:myRole,name:n,bio:document.getElementById('p-bio').value,canon:document.getElementById('p-canon').value}); }
  function saveMaster(){ socket.emit('save_master_base',{title:document.getElementById('m-title').value,sys:document.getElementById('m-sys').value,pro:document.getElementById('m-pro').value,sum:document.getElementById('m-sum').value,model:document.getElementById('m-ai-model').value,solo_mode:document.getElementById('m-solo').value==="true"}); alert("저장됨"); }
  function startSession(){ socket.emit('start_session'); }
  function sessionReset(){ if(confirm("초기화?")) socket.emit('reset_session',{password:prompt("PW:")}); }
  function saveExamples(){ const exs=[]; for(let i=0;i<3;i++) exs.push({q:document.getElementById(`ex-q-${i}`).value,a:document.getElementById(`ex-a-${i}`).value}); socket.emit('save_examples',exs); alert("저장됨"); }
  function addLoreWithTags(){ socket.emit('add_lore',{title:document.getElementById('kw-t').value,triggers:document.getElementById('tag-input').value||"",content:document.getElementById('kw-c').value,index:document.getElementById('kw-index').value}); clearLoreEditor(); }
  function editLore(i){ const l=gState.lorebook[i]; document.getElementById('kw-t').value=l.title; document.getElementById('kw-c').value=l.content; document.getElementById('kw-index').value=i; } // Note: triggers logic simplified for brevity
  function delLore(i){ socket.emit('del_lore',{index:i}); }
  function uploadFile(i){ const fd=new FormData(); fd.append('file',i.files[0]); fetch('/import',{method:'POST',body:fd}).then(()=>alert("복원됨")); }
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
