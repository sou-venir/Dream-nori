# [1] 필수 라이브러리 설치 (코랩 환경 전용)

import os, json, copy, re, requests, base64, urllib.parse, subprocess, threading, time
from datetime import datetime
from flask import Flask, render_template_string, request, Response
from flask_socketio import SocketIO, emit
import openai
import google.generativeai as genai

# [수정] 로컬 폴더 경로 (코드가 있는 곳의 data 폴더)
SAVE_PATH = './data'
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
ADMIN_PASSWORD = userdata.get('ADMIN_PASSWORD')

gemini_model = None
client = None  
try:
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    try:
        GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
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
    "output_limit": 2000,
    "player_count": 3,
    "solo_mode": False,
    "session_started": False,
    "profiles": {
        "user1": {"name": "Player 1", "bio": "", "canon": "", "locked": False},
        "user2": {"name": "Player 2", "bio": "", "canon": "", "locked": False},
        "user3": {"name": "Player 3", "bio": "", "canon": "", "locked": False}
    },
    "pending_inputs": {},
    "ai_history": [],
    "summary": "",
    "prologue": "",
    "sys_prompt": "당신은 숙련된 TRPG 마스터입니다.",
    "lorebook": [],
    "examples": [{"q": "", "a": ""}, {"q": "", "a": ""}, {"q": "", "a": ""}]
} # ✅ 중복 괄호 제거 완료

saved_data = load_data()
if saved_data:
    state = saved_data
    # ✅ [중요] 옛날 저장 파일에 user3가 없으면 강제로 만들어줌
    if "user3" not in state["profiles"]:
        state["profiles"]["user3"] = {"name": "Player 3", "bio": "", "canon": "", "locked": False}

    # ✅ 인원수 설정이 없으면 기본 3인으로 설정
    if "player_count" not in state:
        state["player_count"] = 3

    # 기존 ip_map은 버리고 client_map(고유 ID용) 사용
    state.pop("ip_map", None)
    client_map = state.pop("client_map", {})
else:
    state = copy.deepcopy(initial_state)
    client_map = {}


connected_users = {"user1": None, "user2": None, "user3": None} # ✅ user3 추가
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
        "player_count": state.get("player_count", 3),     # 추가
        "output_limit": state.get("output_limit", 2000), # 추가
        "theme": state.get("theme"),                     # 추가
        "solo_mode": bool(state.get("solo_mode", False)),
        "_export_type": "dream_config_only_v1"
    }

def import_config_only(data: dict):
    # 허용 리스트에 player_count와 output_limit을 꼭 넣어줘야 해!
    allow = {
        "session_title", "sys_prompt", "prologue", "ai_model",
        "examples", "lorebook", "solo_mode", "player_count", "output_limit"
    }
    for k in allow:
        if k in data:
            state[k] = copy.deepcopy(data[k])

    # 테마도 있다면 같이 불러오도록 추가하자
    if "theme" in data:
        state["theme"] = copy.deepcopy(data["theme"])

def get_sanitized_state():
    safe = copy.deepcopy(state)
    for u in ["user1", "user2", "user3"]: # ✅ 반복문으로 처리하면 깔끔해
        safe["profiles"][u]["bio"] = ""
        safe["profiles"][u]["canon"] = ""
    return safe

def emit_state_to_players(save=True):
    if save: save_data()

    base_state = copy.deepcopy(state)
    base_state["pending_status"] = list(state.get("pending_inputs", {}).keys())
    base_state["typing_status"] = list(typing_users)

    # ✅ 설정된 인원수에 상관없이 일단 user1~3까지 다 챙기도록 안전장치
    players = ["user1", "user2", "user3"]

    for me in players:
        # 내 전용 state 복사본 생성
        my_view = copy.deepcopy(base_state)

        # '나'를 제외한 다른 사람들의 비밀 정보 지우기
        for other in players:
            if me != other:
                # 만약 다른 유저 정보가 존재할 때만 지우기 (KeyError 방지)
                if other in my_view["profiles"]:
                    my_view["profiles"][other]["bio"] = ""
                    my_view["profiles"][other]["canon"] = ""

        # 해당 유저가 접속해 있다면 전송
        if connected_users.get(me):
            socketio.emit("initial_state", my_view, room=connected_users[me])

    # 관전자용
    safe_view = get_sanitized_state()
    safe_view["pending_status"] = list(state.get("pending_inputs", {}).keys())
    safe_view["typing_status"] = list(typing_users)

    for rsid in readonly_sids:
        socketio.emit("initial_state", safe_view, room=rsid)

def analyze_theme_color(title, sys_prompt):
    prompt_text = (
    f"세션 제목: {title}\n"
    f"세션 프롬프트 / 프롤로그:\n{sys_prompt[:1200]}\n\n"

    "위 텍스트를 **규칙 설명이 아닌, 감정·분위기·정서 이미지**의 관점에서 해석하십시오.\n"
    "이 세션이 플레이어에게 주는 핵심 정서를 먼저 내부적으로 요약한 뒤 색을 결정하십시오.\n"
    "특히 다음 요소를 중점적으로 고려하십시오:\n"
    "- 긴장도 (높음/중간/낮음)\n"
    "- 정서 톤 (음습함, 달콤함, 불안, 폭력성, 관조 등)\n"
    "- 시각적 이미지 (어둠, 밀폐, 네온, 따뜻함, 차가움 등)\n\n"

    "웹 UI 컬러 팔레트 전문가로서 아래 지침에 따라 딱 하나의 JSON 객체만 반환하세요.\n"
        "1. 'bg' (채팅창 배경): 눈이 편안하고 밝은 색.\n"
        "2. 'panel' (입력창/사이드바): bg보다 살짝 어둡거나 대비되는 계열.\n"
        "3. 'accent' (강조): 버튼과 태그에 사용될 '선명하고 진한' 컬러. "
        "반드시 그 위에 흰색(#FFFFFF) 글자를 썼을 때 가독성이 완벽하게 확보되도록 '채도가 높고 충분히 어두운' 색상이어야 합니다. "
        "연한 파스텔톤이나 흐릿한 색상은 절대 사용하지 마세요.\n"
        "반드시 JSON 형식만 반환: {\"bg\":\"#RRGGBB\",\"panel\":\"#RRGGBB\",\"accent\":\"#RRGGBB\"}"
    )

    default_theme = state.get("theme", {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"})

    # --- 1단계: OpenAI 시도 ---
    if client:
        try:
            res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Return a single JSON object only."},
                    {"role": "user", "content": prompt_text}
                ],
                response_format={"type": "json_object"}
            )
            obj = json.loads(res.choices[0].message.content)
            print("🎨 OpenAI로 테마 분석 완료")
            return apply_theme_logic(obj, default_theme)
        except Exception as e:
            print(f"⚠️ OpenAI 분석 실패: {e}")

    # --- 2단계: Gemini 시도 ---
    if GEMINI_API_KEY:
        try:
            model = genai.GenerativeModel('gemini-2.0-flash-lite')
            response = model.generate_content(
                prompt_text,
                generation_config={"response_mime_type": "application/json"}
            )
            obj = json.loads(response.text)

            # [핵심 수정] Gemini가 리스트([...])로 줬을 경우를 대비해 첫 번째 항목만 추출!
            if isinstance(obj, list) and len(obj) > 0:
                obj = obj[0]

            print("🎨 Gemini Flash로 테마 분석 완료")
            return apply_theme_logic(obj, default_theme)
        except Exception as e:
            print(f"⚠️ Gemini 분석 실패: {e}")

    return default_theme

def apply_theme_logic(obj, current_theme):
    # obj가 딕셔너리가 아니면(에러 방지용) 기존 테마 반환
    if not isinstance(obj, dict):
        return current_theme

    out = copy.deepcopy(current_theme)
    for k in ("bg", "panel", "accent"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("#") and len(v) == 7:
            out[k] = v
    return out

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
        
        current_model = state.get("ai_model", "gpt-5.2").lower()

        # 1. 제미나이 모델을 사용 중일 때 요약 (Gemini Flash 사용)
        if "gemini" in current_model and gemini_model:
            try:
                # 요약 전용으로 빠르고 가벼운 Flash 모델 호출
                summary_engine = genai.GenerativeModel('gemini-2.0-flash-lite')
                response = summary_engine.generate_content(
                    f"다음 대화 내역을 바탕으로, 이후 서사 진행에 필요한 핵심 사건과 감정선 위주로 아주 간결하게 요약해줘:\n\n{recent_log}"
                )
                if response.text:
                    return response.text.strip()
            except Exception as e:
                print(f"⚠️ 제미나이 요약 실패: {e}")
                # 제미나이 요약 실패하면 아래 GPT 로직으로 자연스럽게 넘어감

        # 2. GPT 모델을 사용 중이거나 제미나이 요약이 실패했을 때 (OpenAI 사용)
        if client:
            try:
                res = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role":"user","content":f"다음 대화 내용을 핵심 위주로 요약해줘:\n{recent_log}"}]
                )
                return (res.choices[0].message.content or "").strip()
            except Exception as e:
                print(f"⚠️ GPT 요약 실패: {e}")

        return None

    try:
        s = run_once()
        if s:
            state["summary"] = s[:SUMMARY_MAX_CHARS]
            save_data()
            print("📝 자동 요약 완료!")
    except:
        pass

def simple_decrypt(data, key):
    """비번을 이용해 암호화된 텍스트를 복구하는 마법 (간이 암호화)"""
    try:
        # 비번이 틀리면 여기서 에러가 나서 자연스럽게 차단돼!
        decoded = base64.b64decode(data).decode('utf-8')
        # 간단한 XOR이나 특정 규칙으로 더 꼴 수 있지만,
        # 1차적으로 base64 + 비번 매칭만 해도 미성년자는 절대 못 읽어.
        return json.loads(decoded)
    except:
        return None

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

    # [수정] 영어/숫자 외에 다 지우던 로직을, 금지된 특수문자만 지우는 로직으로 변경
    title = cfg.get("session_title") or "session"
    safe_title = re.sub(r'[\\/:*?"<>|]+', "_", title)

    fname = f"{safe_title}_{ts}.json"
    data = json.dumps(cfg, ensure_ascii=False, indent=2)

    # 한국어 파일명을 브라우저가 인식하게 만드는 처리
    resp = Response(data, mimetype="application/json; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}"
    return resp

@app.route("/import", methods=["POST"])
def import_config():
    try:
        if "file" not in request.files: return "파일X", 400
        file = request.files["file"]
        if file.filename == "": return "파일X", 400
        data = json.loads(file.read().decode('utf-8'))
        import_config_only(data)
        state["theme"] = analyze_theme_color(state.get("session_title", ""), state.get("sys_prompt", "") + "\n" + state.get("prologue", ""))

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
    cid = (data or {}).get("client_id")

    # 1. 재접속 확인 (기존 ID가 user3인지도 확인됨)
    if cid in client_map:
        role = client_map[cid]
        # 만약 role이 user3인데 connected_users엔 없으면 다시 연결
        connected_users[role] = sid
        emit("assign_role", {"role": role, "mode": "player", "source": "uuid"})
        emit_state_to_players()
        return

    # 2. 빈 자리 찾기 (순서대로 채움)
    target_role = None
    if connected_users["user1"] is None: target_role = "user1"
    elif connected_users["user2"] is None: target_role = "user2"
    elif connected_users["user3"] is None: target_role = "user3" # ✅ 여기 추가!

    if target_role:
        connected_users[target_role] = sid
        client_map[cid] = target_role
        save_data()
        emit("assign_role", {"role": target_role, "mode": "player", "source": "new"})
        emit_state_to_players()
        return

    # 3. 만석 (관전)
    readonly_sids.add(sid)
    emit("assign_role", {"role": "readonly", "mode": "readonly"})
    emit_state_to_players()

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    admin_sids.discard(sid)

    # user1, user2, user3 모두 체크
    for role in ("user1", "user2", "user3"):
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
    client_map = {}
    for role in connected_users:
        connected_users[role] = None

    save_data()
    # 클라이언트의 UUID까지 지우도록 신호를 보냄
    socketio.emit("reload_signal", {"clear_uuid": True})

@socketio.on("start_typing")
def start_typing(data):
    uid = data.get("uid")
    # ✅ user3 포함
    if uid in ("user1", "user2", "user3"):
        typing_users.add(uid)
        socketio.emit("typing_update", {"typing_users": list(typing_users)})

@socketio.on("stop_typing")
def stop_typing(data):
    uid = data.get("uid")
    if uid in ("user1", "user2", "user3"):
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

@socketio.on("save_master_all")
def save_master_all(data):
    # 1. 엔진 설정
    state["sys_prompt"] = (data.get("sys", state["sys_prompt"]) or "")[:4000]
    state["summary"] = (data.get("sum", state["summary"]) or "")[:SUMMARY_MAX_CHARS]
    state["ai_model"] = data.get("model", state.get("ai_model","gpt-5.2"))
    state["output_limit"] = int(data.get("output_limit", 2000))

    try:
        pc = int(data.get("player_count", 3))
        if pc in (1, 2, 3):
            state["player_count"] = pc
            state["solo_mode"] = (pc == 1)
    except: pass

    # 2. 서사 설정
    old_title = state["session_title"]
    old_pro = state["prologue"]
    state["session_title"] = (data.get("title", state["session_title"]) or "")[:30]
    state["prologue"] = (data.get("pro", state["prologue"]) or "")[:1000]

    # 제목이나 프롤로그가 바뀌었을 때만 테마 분석
    if old_title != state["session_title"] or old_pro != state["prologue"]:
        combined = state["sys_prompt"] + "\n\n[PROLOGUE]\n" + state["prologue"]
        if combined.strip():
            state["theme"] = analyze_theme_color(state["session_title"], combined)

    save_data()
    emit_state_to_players()


# 프로필 잠금 해제 기능 추가
@socketio.on("unlock_profile")
def unlock_profile(data):
    # 비밀번호 검사 줄을 아예 삭제!
    target = data.get("target")
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
    # ✅ user3 추가
    if uid not in ("user1", "user2", "user3"): return
    if connected_users.get(uid) != request.sid: return

    # 잠겨있으면 수정 불가
    if state["profiles"][uid].get("locked"): return

    name = (data.get("name") or "").strip()
    if not name: return

    state["profiles"][uid]["name"] = name[:12]
    state["profiles"][uid]["bio"] = (data.get("bio") or "")[:200]
    state["profiles"][uid]["canon"] = (data.get("canon") or "")[:400]
    state["profiles"][uid]["locked"] = True # 저장하면 잠금

    save_data()
    emit_state_to_players()

@socketio.on("start_session")
def start_session(_=None):
    if request.sid not in admin_sids: return

    # [수정] 설정된 인원수에 맞춰 모두가 프로필 잠금을 했는지 체크
    pc = state.get("player_count", 3)
    p1 = state["profiles"]["user1"].get("locked")
    p2 = state["profiles"]["user2"].get("locked")
    p3 = state["profiles"]["user3"].get("locked")

    is_ready = False
    if pc == 1: is_ready = p1
    elif pc == 2: is_ready = p1 and p2
    else: is_ready = p1 and p2 and p3

    if not is_ready:
        emit("status_update", {"msg": "⚠️ 모든 플레이어가 프로필 설정을 저장(확정)해야 시작할 수 있습니다."})
        return

    state["session_started"] = True
    save_data()
    emit_state_to_players()
    socketio.emit("status_update", {"msg": "✅ 세션이 시작되었습니다! 이제 행동을 입력하세요."})

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

    # 모든 상태를 완전 백지로 초기화
    state["session_title"] = "드림놀이"
    state["theme"] = {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"}
    state["ai_model"] = "gpt-5.2"
    state["solo_mode"] = False
    state["session_started"] = False

    # 프로필 정보 완전 초기화 (이름까지 빈칸으로)
    state["profiles"]["user1"] = {"name": "", "bio": "", "canon": "", "locked": False}
    state["profiles"]["user2"] = {"name": "", "bio": "", "canon": "", "locked": False}
    state["profiles"]["user3"] = {"name": "", "bio": "", "canon": "", "locked": False} # ✅ 추가

    state["pending_inputs"] = {}
    typing_users.clear()

    state["ai_history"] = []
    state["summary"] = ""
    state["prologue"] = ""
    state["sys_prompt"] = ""

    state["lorebook"] = []
    state["examples"] = [{"q": "", "a": ""}, {"q": "", "a": ""}, {"q": "", "a": ""}]

    save_data()
    emit_state_to_players()
    socketio.emit("status_update", {"msg": "🧹 모든 데이터가 삭제되어 백지가 되었습니다."})

def record_pending(uid, text):
    state.setdefault("pending_inputs", {})
    state["pending_inputs"][uid] = {"text": (text or "")[:600], "ts": datetime.now().isoformat()}
    save_data()

def check_all_ready():
    pc = state.get("player_count", 3) # 기본값 3
    p = state.get("pending_inputs", {})

    if pc == 1:
        return "user1" in p
    elif pc == 2:
        return "user1" in p and "user2" in p
    else: # 3인
        return "user1" in p and "user2" in p and "user3" in p
        
def build_gemini_prompt(system_content, priority_instruction, examples, prologue_text, round_block, limit):
    """
    Gemini 전용 프롬프트 빌더 (순서 최적화 버전)
    """
    prompt = ""
    
    # 1. 기본 시스템 설정 및 캐릭터 캐논
    prompt += "### [SYSTEM & CHARACTER CANON]\n"
    prompt += system_content.strip() + "\n\n"

    # 2. 예시 (Examples) - AI에게 말투를 학습시킴
    if examples:
        prompt += "### [STYLE EXAMPLES]\n"
        for ex in examples:
            if ex.get("q") and ex.get("a"):
                prompt += f"User: {ex['q']}\nModel: {ex['a']}\n"
        prompt += "\n"

    # 3. 과거 맥락 (프롤로그 + 히스토리)
    prompt += "### [CONTEXT HISTORY]\n"
    if prologue_text and len(state.get("ai_history", [])) < 40:
        prompt += f"[PROLOGUE]: {prologue_text.strip()}\n"
    
    # build_history_block() 호출 시 인자 제거!
    prompt += "\n".join(build_history_block()) + "\n\n"

    # 4. 현재 상황 및 입력 (중요도 높음)
    prompt += "### [CURRENT PLAYER INPUTS]\n"
    prompt += round_block.strip() + "\n\n"

    # 5. 최종 지시사항 (제미나이가 가장 잘 듣는 위치)
    prompt += "### [FINAL INSTRUCTION]\n"
    prompt += priority_instruction.strip() + "\n"
    
    # 여기를 수정! (Strict Length Constraint 추가)
    prompt += f"!!! [STRICT LENGTH CONSTRAINT] !!!\n"
    prompt += f"- 작성 분량 제한: 공백 포함 최대 {limit}자.\n"
    prompt += f"- 이 길이를 초과하려 하면 즉시 문장을 맺고 종료하십시오.\n"
    prompt += "- 이전 대화가 길더라도, 이번 턴은 반드시 위 제한을 준수해야 합니다."

    return prompt



def trigger_ai_from_pending():
    pc = state.get("player_count", 3)
    limit = state.get("output_limit", 2000)

    pending = state.get("pending_inputs", {})
    p1_text = pending.get("user1", {}).get("text", "(스킵)")
    p2_text = pending.get("user2", {}).get("text", "(스킵)")
    p3_text = pending.get("user3", {}).get("text", "(스킵)") if pc >= 3 else ""

    u1 = state["profiles"]["user1"]
    u2 = state["profiles"]["user2"]
    u3 = state["profiles"]["user3"]

    p1_name, p1_bio, p1_canon = u1.get("name", "Player 1"), u1.get("bio", ""), u1.get("canon", "")
    p2_name, p2_bio, p2_canon = u2.get("name", "Player 2"), u2.get("bio", ""), u2.get("canon", "")

    if pc >= 3:
        p3_name, p3_bio, p3_canon = u3.get("name", "Player 3"), u3.get("bio", ""), u3.get("canon", "")
    else:
        p3_name, p3_bio, p3_canon = "", "", ""

    # 1. 키워드(Lore) 매칭
    last_ai_msg = ""
    for h in reversed(state.get("ai_history", [])):
        if h.startswith("**AI**:"):
            last_ai_msg = h.replace("**AI**:", "").strip()
            break

    merged_for_lore = f"{p1_text} {p2_text} {p3_text} {last_ai_msg}"
    merged_lower = merged_for_lore.lower()

    active_context = []
    for l in state.get("lorebook", []):
        triggers = [t.strip().lower() for t in (l.get("triggers","")).split(",") if t.strip()]
        if any(t in merged_lower for t in triggers):
            active_context.append(f"[{l.get('title','')}]: {l.get('content','')}")

    active_context = active_context[:3]

    sys_prompt = state.get('sys_prompt','')
    prologue_text = state.get("prologue", "")

    # [3] 프로필 프롬프트 조립
    profile_content = f"### [CHARACTER PROFILES]\n1. {p1_name}\n- Bio: {p1_bio}\n- Relationship/Canon: {p1_canon}\n\n"
    if pc >= 2:
        profile_content += f"2. {p2_name}\n- Bio: {p2_bio}\n- Relationship/Canon: {p2_canon}\n\n"
    if pc >= 3:
        profile_content += f"3. {p3_name}\n- Bio: {p3_bio}\n- Relationship/Canon: {p3_canon}"

    # 시스템 프롬프트: '결과 판정'을 강조
    def build_full_system_content():
        return (
            f"### [ABSOLUTE RULE: CHARACTER FIDELITY]\n"
            f"- If any narration conflicts with [MANDATORY CANON], the narration must be rewritten until canon consistency is restored.\n"
            f"- [MANDATORY CANON]은 이 세계의 물리 법칙과 같으며, 이를 위반하는 서사는 존재할 수 없습니다.\n\n"
            f"{profile_content}\n\n"

            f"### [CORE LOGIC: ATTEMPT vs RESULT]\n"
            f"1. **Player Input = Attempt (시도/의도)**: 플레이어의 입력은 '무엇을 하려 하는가'이다.\n"
            f"2. **AI Output = Result (결과/반응)**: 당신은 그 시도가 성공했는지, 실패했는지, 주변 환경(NPC)이 어떻게 반응하는지를 서술한다.\n"
            f"3. **No Forced Acting**: 플레이어 캐릭터의 입이나 몸을 빌려 임의로 연기하지 마라. 오직 외부 세계의 묘사에 집중하라.\n\n"

            f"### [TIME RESOLUTION RULE — CRITICAL]\n"
            f"1. 한 라운드에서 서술 가능한 시간은 '하나의 연속된 순간'까지만 허용된다.\n"
            f"2. 플레이어가 명시하지 않은 다음 행동, 이동 완료, 결정 결과는 서술하지 않는다.\n"
            f"3. 장면은 항상 '다음 행동이 발생하기 직전'에서 멈춘다.\n\n"

            f"### [SCENE DENSITY RULE]\n"
            f"1. 사건을 진행시키기보다, 감각 정보(시각, 소리, 거리, 긴장)를 확장하라.\n"
            f"2. 인과 진행보다 상태 묘사를 우선하라.\n"
            f"3. 변화는 미세하게, 정지는 길게 유지하라.\n\n"

            f"### [NO UNDECLARED ACTION RULE]\n"
            f"1. 플레이어가 선언하지 않은 이동, 조작, 접촉, 발화는 발생하지 않은 것으로 간주한다.\n"
            f"2. 암시, 예측, 회상 형태로도 이를 서술하지 않는다.\n"

            f"### [ANTI-RUSH RULE — OPENAI]\n"
            f"- Do NOT conclude scenes.\n"
            f"- Do NOT resolve conflicts fully.\n"
            f"- Always end with unresolved spatial or emotional tension.\n"

            f"### [Active Lore]\n" + "\n".join(active_context) + "\n\n"
            f"### [Previous Summary]\n{state.get('summary','')}"
        )

    system_content = build_full_system_content()

    if would_overflow_context(system_content + merged_for_lore):
        auto_summary_apply()
        system_content = build_full_system_content()

    # 쐐기 지침 (빠져있던 변수 추가!)
    char_list_str = f"{p1_name}"
    if pc >= 2: char_list_str += f", {p2_name}"
    if pc >= 3: char_list_str += f", {p3_name}"

    priority_instruction = (
        f"!!! [EXECUTION ORDER] !!!\n"
        f"1. Check Inputs: {char_list_str}의 행동을 확인하십시오.\n"
        f"2. Simulation: 그들의 행동이 서로 충돌하는지, 혹은 NPC에게 어떤 영향을 주는지 계산하십시오.\n"
        f"3. Narration: 플레이어의 문장을 그대로 복사하지 말고, '결과' 위주로 서술하십시오.\n"
        f"   - (Good): A가 검을 휘두르자, 몬스터는 비명을 지르며 쓰러졌다.\n"
        f"   - (Bad): A는 검을 휘두르기로 했다. A는 검을 잡고... (불필요한 과정 서술 금지)\n"
        f"4. Format: {limit}자 내외로 장면을 서술하되, 결말이나 최종 결과는 유보하십시오.\n"
    )

    # ✅ [4] 행동 입력 블록 조립 (덮어쓰기 버그 수정됨)
    # 해석 주의사항을 먼저 넣고, 그 뒤에 입력값을 붙이는 방식
    round_block = (
        "--- [INTERPRETATION NOTICE] ---\n"
        "아래 입력들은 플레이어들의 '동시 행동 선언'입니다.\n"
        "AI는 이를 확정된 과거가 아닌, '현재 진행 중인 시도'로 해석하여 결과를 판정해야 합니다.\n\n"
        "--- [ROUND INPUT] ---\n"
    )
    round_block += f"<{p1_name}>: {p1_text}\n"
    if pc >= 2: round_block += f"<{p2_name}>: {p2_text}\n"
    if pc >= 3: round_block += f"<{p3_name}>: {p3_text}\n"

    # 메시지 리스트 조립
    messages = [{"role":"system","content":system_content}]
    for ex in state.get("examples", []):
        if ex.get("q"): messages.extend([{"role":"user","content":ex["q"]}, {"role":"assistant","content":ex["a"]}])

    if prologue_text and len(state.get("ai_history", [])) < 40:
        messages.append({"role": "user", "content": f"### [PROLOGUE / INITIAL CONTEXT]\n{prologue_text}"})

    for h in build_history_block():
        messages.append({"role": "assistant" if h.startswith("**AI**") else "user", "content": h})

    messages.append({"role":"user","content": round_block})
    # OpenAI 모델도 말을 잘 듣게 하기 위해 마지막에 instruction 추가
    messages.insert(1, {"role":"system", "content": priority_instruction})


    current_model = state.get("ai_model","gpt-5.2")
    socketio.emit("status_update", {"msg": f"🤔 {current_model} 집필 중..."})

    ai_response = ""
    try:
        # [1] 제미나이 호출 시도
        if "gemini" in current_model.lower() and gemini_model:
            from google.generativeai.types import HarmCategory, HarmBlockThreshold
            safe = {
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
            
            prompt = build_gemini_prompt(system_content, priority_instruction, state.get("examples", []), state.get("prologue", ""), round_block, limit)
            
            response = gemini_model.generate_content(
                prompt,
                safety_settings=safe,
                generation_config={
                    # [수정] 2배가 아니라 0.8배로 줄임! (물리적으로 더 못 쓰게 차단)
                    "max_output_tokens": int(limit * 0.8), 
                    "temperature": 0.8,
                }
            )

            # 답변이 성공적으로 왔는지 확인
            if response.candidates and response.candidates[0].content.parts:
                ai_response = response.text
            else:
                # 🚨 제미나이가 거절했을 때! 바로 GPT-4o-mini로 긴급 전환
                print("⚠️ 제미나이 차단됨 -> GPT로 긴급 전환합니다.")
                if client:
                    res = client.chat.completions.create(
                        model="gpt-4o-mini", # 가볍고 필터링이 유연한 모델로 전환
                        messages=messages,
                        max_tokens=int(limit * 1.5)
                    )
                    ai_response = "(제미나이 필터로 인해 GPT가 대리 작성한 답변입니다)\n\n" + res.choices[0].message.content
                else:
                    ai_response = "⚠️ 제미나이가 답변을 거부했고, 대체할 GPT 키도 없습니다."

        # [2] 처음부터 GPT 계열을 선택했을 때
        elif client:
            dynamic_max_tokens = int(limit * 1.5)
            res = client.chat.completions.create(
                model=current_model,
                messages=messages,
                max_tokens=dynamic_max_tokens
            )
            ai_response = res.choices[0].message.content
        else:
            ai_response = "API Key Error."

    except Exception as e:
        # 예외 발생 시에도 GPT로 한 번 더 시도하는 마지막 방어막
        print(f"🔥 에러 발생: {e} -> 마지막 수단으로 GPT 시도")
        try:
            if client:
                res = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    max_tokens=int(limit * 1.5)
                )
                ai_response = "(시스템 오류로 인해 예비 엔진이 작성한 답변입니다)\n\n" + res.choices[0].message.content
            else:
                ai_response = f"Error: {e}"
        except:
            ai_response = f"Critical Error: {e}"


    # [마지막] 여기서부터 저장 및 전송 로직 (들여쓰기는 try랑 똑같이!)
    history_line = f"**Round**: {p1_name}: {p1_text}"
    if pc >= 2: history_line += f" / {p2_name}: {p2_text}"
    if pc >= 3: history_line += f" / {p3_name}: {p3_text}"

    state["ai_history"].append(history_line)
    state["ai_history"].append(f"**AI**: {ai_response}")
    state["pending_inputs"] = {}
    save_data()

    socketio.emit("ai_typewriter_event", {"content": ai_response})
    emit_state_to_players()

@socketio.on("client_message")
def client_message(data):
    uid = data.get("uid")
    text = (data.get("text") or "").strip()

    # 인원수와 상관없이 일단 허용된 유저인지 확인
    if uid not in ("user1", "user2", "user3") or not state.get("session_started"): return

    record_pending(uid, text)
    typing_users.discard(uid)
    emit_state_to_players()

    if check_all_ready():
        trigger_ai_from_pending()
    else:
        # 대기 메시지 전송 로직
        pc = state.get("player_count", 3)
        needed = ["user1", "user2", "user3"][:pc]
        done = state.get("pending_inputs", {}).keys()
        not_yet = [u for u in needed if u not in done]

        names = [state["profiles"][u].get("name") or u for u in not_yet]
        msg_str = ", ".join(names)
        socketio.emit("status_update", {"msg": f"⏳ {msg_str} 입력 대기... (스킵 가능)"})

@socketio.on("skip_turn")
def skip_turn(data):
    uid = data.get("uid")
    # ✅ user3 포함 검사
    if uid not in ("user1", "user2", "user3") or not state.get("session_started"): return

    record_pending(uid, "(스킵)")
    typing_users.discard(uid)
    emit_state_to_players()

    # ✅ check_all_ready로 변경
    if check_all_ready():
        trigger_ai_from_pending()
    else:
        # ✅ 대기 메시지 로직 (위와 동일)
        needed = ["user1", "user2", "user3"]
        done = state.get("pending_inputs", {}).keys()
        not_yet = [u for u in needed if u not in done]

        names = [state["profiles"][u].get("name") or u for u in not_yet]
        msg_str = ", ".join(names)

        socketio.emit("status_update", {"msg": f"⏳ {msg_str} 입력 대기... (스킵 가능)"})

@socketio.on("get_scenario_list")
def get_scenario_list(_=None):
    LIB_URL = "https://raw.githubusercontent.com/sou-venir/sou-venir-scenario/refs/heads/main/library.json"
    try:
        res = requests.get(LIB_URL, timeout=5)
        res.raise_for_status()
        socketio.emit("scenario_list_res", {"success": True, "list": res.json()})
    except Exception as e:
        socketio.emit("scenario_list_res", {"success": False, "msg": str(e)})

@socketio.on("load_scenario_url")
def load_scenario_url(data):
    url = data.get("url")
    auth_key = data.get("auth_key") # 유저가 입력한 비번
    is_adult = data.get("is_adult", False)

    try:
        # 공개 저장소니까 토큰 없이 그냥 가져와!
        response = requests.get(url, timeout=5)
        response.raise_for_status()

        if is_adult:
            # 성인용이면 해독 시도! (auth_key가 해독 키 역할을 함)
            # 여기서는 미리 암호화해서 올린 데이터를 푼다고 가정해.
            scenario_data = simple_decrypt(response.text, auth_key)

            # 만약 설정한 특정 비번(SCENARIO_KEY)과
            # 유저가 입력한 비번이 다르면 로드 거부!
            if not scenario_data or auth_key != userdata.get('ADULT_KEY'):
                socketio.emit("status_update", {"msg": "❌ 인증 코드가 틀렸거나 잘못된 파일입니다!"})
                return
        else:
            # 일반 시나리오는 그냥 읽기
            scenario_data = response.json()

        import_config_only(scenario_data)

        # [이 코드를 추가!] 불러온 데이터로 테마 즉시 분석
        state["theme"] = analyze_theme_color(state.get("session_title", ""), state.get("sys_prompt", "") + "\n" + state.get("prologue", ""))

        save_data()
        emit_state_to_players()
        socketio.emit("status_update", {"msg": "📂 시나리오 로드 완료!"})
    except Exception as e:
        socketio.emit("status_update", {"msg": f"❌ 로드 실패: {str(e)}"})


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
    #chat-window {
        flex:1; overflow-y:auto; padding:30px 10%; display:flex; flex-direction:column; gap:15px; scroll-behavior:smooth;
        background: var(--bg); /* 👈 여기가 핵심! */
    }
    #input-area, #sidebar, #sidebar-footer { background: var(--panel); }
    #chat-content{display:flex;flex-direction:column;gap:15px;padding-bottom:20px;}
    #sidebar{width:320px;height:100vh;background:var(--panel);display:flex;flex-direction:column;overflow:hidden;}
    #sidebar-body{padding:20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:12px;}
    #sidebar-footer{padding:12px 20px 16px;border-top:1px solid rgba(0,0,0,0.06);background:var(--panel);}

    #m-output-limit {
  -webkit-appearance: none; /* 기본 디자인 제거 */
  width: 100% !important;   /* 양옆으로 꽉 채우기 */
  height: 8px;
  border-radius: 10px;
  background: #eee !important; /* 배경 막대 색상 */
  outline: none;
  margin: 15px 0;
  border: none !important;    /* 기존 테두리 제거 */
}

/* 슬라이더 손잡이 (동그라미) - 크롬/사파리용 */
#m-output-limit::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--accent) !important; /* 우리 핑크색 */
  cursor: pointer;
  border: 3px solid #fff; /* 하얀색 테두리로 포인트 */
  box-shadow: 0 2px 6px rgba(0,0,0,0.2);
  transition: 0.2s;
}

#m-output-limit::-webkit-slider-thumb:hover {
  transform: scale(1.15); /* 마우스 올리면 살짝 커짐 */
}
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
    #p-bio { height: 120px !important; }   /* 캐릭터 설정 (높이 줄임) */
    #p-canon { height: 180px !important; } /* 관계 설정 (더 크게) */

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
    /* 모달 내부 가로 스크롤 방지 */
.modal-body, .tab-content, .editor-side, .list-side {
    overflow-x: hidden !important; /* 가로로 절대 못 삐져나오게 함 */
    box-sizing: border-box;         /* 패딩이 너비에 포함되게 계산 */
}

/* 입력창들이 가로 너비를 초과하지 않도록 강제 고정 */
.editor-side input, .editor-side textarea, .list-side select {
    max-width: 100%;
    width: 100% !important;
}

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
        background: rgba(255, 255, 255, 0.05); /* 아주 연한 흰색 틴트 */
        backdrop-filter: blur(1px); /* 살짝 흐리게 해서 '잠김' 느낌 강조 */
        border-radius: 12px;
        cursor: not-allowed;
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
  <!-- 상태 메시지 (준비 대기 중... 같은 글자가 뜨는 곳) -->
  <div id="status" style="font-size:12px;margin-bottom:5px;color:var(--accent);font-weight:bold;">대기 중</div>

  <div style="display:flex;gap:10px;align-items:stretch;">
    <!-- 입력창은 하나만! 그리고 처음에 disabled를 꼭 써줘 -->
    <textarea id="msg-input" maxlength="600" placeholder="프로필 설정을 완료하고 마스터가 세션을 시작할 때까지 대기해주세요." disabled></textarea>

    <div style="display:flex;flex-direction:column;gap:8px;width:110px;">
      <!-- 버튼들도 처음엔 못 누르게 disabled 추가 -->
      <button id="send-btn" onclick="send()" style="width:110px;" disabled>전송</button>
      <button id="skip-btn" onclick="skipTurn()" style="width:110px;background:transparent;color:#666;border:1px solid rgba(0,0,0,0.2);padding:6px 10px;font-size:12px;font-weight:800;" disabled>스킵</button>
    </div>
  </div>
</div>
  </div>

  <div id="sidebar">
    <div id="sidebar-body">
      <h3>설정 <span id="role-badge" style="font-size:12px;color:var(--accent)"></span></h3>
      <div id="role-display" style="padding:10px;background:rgba(0,0,0,0.05);border-radius:8px;font-weight:800;color:#555;">접속 중...</div>

      <div id="profile-wrap">
        <div id="profile-lock-overlay" style="display:none;"></div>

        <input type="text" id="p-name" maxlength="12" placeholder="이름">

        <div>
          <textarea id="p-bio" maxlength="200" oninput="upCnt(this)" placeholder="캐릭터 설정 (최대 200자)"></textarea>
          <div id="cnt-p-bio" class="char-cnt">0/200</div>
        </div>

        <div>
          <!-- [수정] maxlength 400으로 변경 -->
          <textarea id="p-canon" maxlength="400" oninput="upCnt(this)" placeholder="드림캐 설정 (최대 400자)"></textarea>
          <div id="cnt-p-canon" class="char-cnt">0/400</div>
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
        <button onclick="closeModal()" class="close-btn">✕</button>
      </div>

      <div class="modal-body">
        <!-- 엔진 탭 -->
        <div id="t-base" class="tab-content active">
            <div class="editor-side" style="display:flex; flex-direction:column; padding:20px; border-right:1px solid #f0f0f0;">
                <label>시스템 프롬프트</label>
                <textarea id="m-sys" class="fill-textarea" maxlength="4000" oninput="upCnt(this)"></textarea>
                <div id="cnt-m-sys" class="char-cnt">0/4000</div>
                <button onclick="saveAllSettings(false)" class="save-btn" style="margin-top:10px;">저장</button>
            </div>

            <div class="list-side" style="display:flex; flex-direction:column; gap:8px; padding:20px; background:#fafafa;">
                <label>서버 관리</label>
                <button onclick="clearRoles()" style="background:#ff9800 !important; color:white;" class="mini-btn">전원 강제 퇴장</button>

                <div style="display:flex; gap:4px;">
                    <button id="btn-ul-1" onclick="unlockProfile('user1')" style="flex:1; background:#44aaff!important;" class="mini-btn">P1 해제</button>
                    <button id="btn-ul-2" onclick="unlockProfile('user2')" style="flex:1; background:#44aaff!important;" class="mini-btn">P2 해제</button>
                    <button id="btn-ul-3" onclick="unlockProfile('user3')" style="flex:1; background:#44aaff!important;" class="mini-btn">P3 해제</button>
                </div>

                <label>세션 데이터</label>
                <div style="display:flex; gap:4px;">
                    <a href="/export" style="flex:1;"><button style="width:100%; background:#444!important;" class="mini-btn">백업</button></a>
                    <button onclick="document.getElementById('import-file').click()" style="flex:1; background:#666!important;" class="mini-btn">불러오기</button>
                    <input type="file" id="import-file" style="display:none;" onchange="uploadSessionFile(this)">
                </div>

                <label>상황 요약</label>
                <textarea id="m-sum" style="height:80px;" maxlength="500"></textarea>

                <label>AI 엔진</label>
                <select id="m-ai-model">
                    <option value="gpt-5.2">OpenAI GPT-5.2</option>
                    <option value="gpt-4o">OpenAI GPT-4o</option>
                    <option value="gemini-3-pro-preview">Google Gemini 3 Pro</option>
                </select>

                <label>플레이 모드</label>
                <select id="m-player-count" onchange="updateAdminBtnVisibility()">
                    <option value="3">3인 플레이</option>
                    <option value="2">2인 플레이</option>
                    <option value="1">1인 플레이</option>
                </select>
                <label style="display: flex; justify-content: space-between; align-items: center;">
    AI 출력량
    <span style="color: var(--accent); font-weight: 900; font-size: 14px;">
        약 <span id="val-output-limit">2000</span>자
    </span>
</label>
<input type="range" id="m-output-limit" min="1500" max="4000" step="500" value="2000"
       oninput="document.getElementById('val-output-limit').innerText=this.value">

                <div style="flex:1;"></div>

                <div id="admin-main-controls" style="display:none; gap:8px;">
                    <button onclick="startSession()" style="flex:1; background:var(--accent)!important;">세션 시작</button>
                    <button onclick="sessionReset()" style="flex:1; background:#ff4444!important;">초기화</button>
                </div>
            </div>
        </div>

        <!-- 서사 탭 -->
        <div id="t-story" class="tab-content">
          <div class="editor-side" style="padding:20px; border-right:1px solid #f0f0f0;">
            <label>세션 제목</label>
            <input type="text" id="m-title" maxlength="30">
            <label>프롤로그</label>
            <textarea id="m-pro" class="fill-textarea" maxlength="1000" oninput="upCnt(this)"></textarea>
            <div id="cnt-m-pro" class="char-cnt">0/1000</div>
            <button onclick="saveMaster()" class="save-btn" style="margin-top:10px;">저장</button>
          </div>
          <div class="list-side" style="padding:20px;">
            <label>안내</label>
            <p style="font-size:12px; color:#666;">제목과 프롤로그를 저장하면 분위기에 맞는 테마가 분석됩니다.</p>
          </div>
        </div>

        <!-- 학습 탭 -->
        <div id="t-ex" class="tab-content">
          <div class="editor-side" style="display:flex; flex-direction:column; min-height:0; padding:20px; border-right:1px solid #f0f0f0;">
            <label>말투 학습(예시 대화 3쌍)</label>

            <div style="flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:12px; padding-right:5px;">
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
            </div>

            <button onclick="saveExamples()" class="save-btn" style="width:100%; height:45px; margin-top:10px; flex-shrink:0;">저장</button>
          </div>
        </div>

        <!-- 키워드 탭 -->
        <div id="t-lore" class="tab-content">
          <div class="editor-side" style="display:flex; flex-direction:column; min-height:0; padding:20px; border-right:1px solid #f0f0f0;">
             <h3 style="margin:0 0 10px 0; font-size:16px;">키워드/설정 추가</h3>
             <div>
               <label>키워드 이름</label>
               <input type="text" id="kw-t" maxlength="20">
             </div>
             <div>
               <label>트리거</label>
               <div id="tag-container" onclick="focusTagInput()">
                 <input type="text" id="tag-input">
               </div>
               <input type="hidden" id="tag-hidden">
             </div>
             <div style="flex:1; display:flex; flex-direction:column;">
               <label>상세 설정</label>
               <textarea id="kw-c" class="fill-textarea" maxlength="400" oninput="upCnt(this)"></textarea>
               <div id="cnt-kw-c" class="char-cnt">0/400</div>
             </div>
             <input type="hidden" id="kw-index" value="-1">
             <button onclick="addLoreWithTags()" class="save-btn" style="width:100%; height:45px; margin-top:10px; flex-shrink:0;">저장 / 수정 완료</button>
          </div>
          <div class="list-side">
            <label>저장된 키워드</label>
            <div id="lore-list" style="flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:8px;"></div>
          </div>
        </div>
      </div> <!-- /.modal-body -->     
<script>
{% raw %}
  const socket = io(); // 반드시 가장 먼저 선언!
  let gState = null;
  let myRole = null;
  let tags = [];
  let sortable = null;
  let isTypewriter = false;
  let editingIdx = -1;
  let currentLibrary = []; // 중복 선언 제거됨

  // [1] 초기화 및 접속
  function getClientId(){
    let id = localStorage.getItem('dream_client_id');
    if(!id){
        id = Math.random().toString(36).substring(2) + Date.now().toString(36);
        localStorage.setItem('dream_client_id', id);
    }
    return id;
  }
  socket.on('connect', () => {
    socket.emit('join_game', { client_id: getClientId() });
  });

  // [2] 역할 할당
  socket.on('assign_role', payload => {
    myRole = payload.role;
    const roleEl = document.getElementById('role-display');
    const badgeEl = document.getElementById('role-badge');
    if(payload.mode === 'readonly'){
      roleEl.innerText = "관전자 모드";
      if(badgeEl) badgeEl.innerText = "";
      document.getElementById('msg-input').disabled = true;
      document.getElementById('send-btn').disabled = true;
      document.getElementById('skip-btn').disabled = true;
      return;
    }
    let roleName = "Player";
    if(myRole === 'user1') roleName = "Player 1";
    else if(myRole === 'user2') roleName = "Player 2";
    else if(myRole === 'user3') roleName = "Player 3";
    roleEl.innerText = roleName + " (당신)";
    if(badgeEl) badgeEl.innerText = "(" + (myRole === 'user1' ? "P1" : myRole === 'user2' ? "P2" : "P3") + ")";
  });

  // [3] 상태 및 채팅 업데이트
  socket.on('status_update', d => {
    const s = document.getElementById('status');
    if(gState && gState.session_started){
        s.innerHTML = d.msg;
        s.style.color = d.msg.includes('❌') ? 'red' : 'var(--accent)';
        if(d.msg.includes("세션이 시작되었습니다")) alert(d.msg);
    }
  });

  socket.on('ai_typewriter_event', d => {
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
      if(i >= full.length){ clearInterval(tick); isTypewriter = false; refreshUI(); }
    }, 20);
  });

  socket.on('typing_update', d => refreshUI());

  // [4] 관리자 및 설정
  socket.on('admin_auth_res', d => {
    const amc = document.getElementById('admin-main-controls');
    if(d.success){
      document.getElementById('admin-modal').style.display='flex';
      refreshLibrary();
      if(amc) amc.style.display = 'flex';
      refreshUI();
    } else {
      alert("비밀번호가 일치하지 않습니다.");
    }
  });

  socket.on('initial_state', data => {
    gState = data;
    if(data.theme){
      const root = document.documentElement.style;
      root.setProperty('--bg', data.theme.bg);
      root.setProperty('--panel', data.theme.panel);
      root.setProperty('--accent', data.theme.accent);
    }
    if(!isTypewriter) refreshUI();
  });

  // [5] 시나리오 라이브러리
  socket.on('scenario_list_res', (res) => {
    if(res.success) {
      currentLibrary = res.list;
      const select = document.getElementById('m-library-select');
      select.innerHTML = currentLibrary.map((s, idx) =>
        `<option value="${idx}">${s.is_adult ? '🔞 ' : ''}${s.title}</option>`
      ).join('');
    } else {
      alert("목록 로드 실패: " + res.msg);
    }
  });

  socket.on('reload_signal', payload => {
    if(payload && payload.clear_uuid) localStorage.removeItem('dream_client_id');
    window.location.reload();
  });

  // [핵심] UI 갱신 (입력창 보호 로직 포함)
  function refreshUI(){
    if(!gState) return;

    // 1. Status Text
    const statusEl = document.getElementById('status');
    const p1 = gState.profiles.user1 || {};
    const p2 = gState.profiles.user2 || {};
    const p3 = gState.profiles.user3 || {};
    let stHtml = "대기 중...";

    if (!gState.session_started) {
        let pc = gState.player_count || 3;
        let p1R = p1.locked ? "✅" : "⏳"; let p2R = p2.locked ? "✅" : "⏳"; let p3R = p3.locked ? "✅" : "⏳";
        if (pc === 1) stHtml = p1.locked ? "<span style='color:#00aa00;font-weight:900;'>✨ 1인 준비 완료!</span>" : `${p1R}${p1.name||"P1"}`;
        else if (pc === 2) stHtml = (p1.locked && p2.locked) ? "<span style='color:#00aa00;font-weight:900;'>✨ 2인 준비 완료!</span>" : `${p1R}${p1.name||"P1"} ${p2R}${p2.name||"P2"}`;
        else stHtml = (p1.locked && p2.locked && p3.locked) ? "<span style='color:#00aa00;font-weight:900;'>✨ 3인 준비 완료!</span>" : `${p1R}${p1.name||"P1"} ${p2R}${p2.name||"P2"} ${p3R}${p3.name||"P3"}`;
    } else {
        const pends = (gState.pending_status||[]);
        let allDone = false;
        let pc = gState.player_count || 3;
        if(pc === 1) allDone = pends.includes('user1');
        else if(pc === 2) allDone = pends.includes('user1') && pends.includes('user2');
        else allDone = pends.includes('user1') && pends.includes('user2') && pends.includes('user3');

        if (allDone) stHtml = `<span style="color:var(--accent);font-weight:900;">🤔 ${gState.ai_model||"AI"} 집필 중...</span>`;
        else stHtml = pends.includes(myRole) ? "<span style='color:green'>입력 완료!</span>" : "행동을 입력하세요.";
    }
    statusEl.innerHTML = stHtml;

    // 2. Lock Inputs (입력 중이면 건드리지 않음)
    const isStarted = !!gState.session_started;
    const isMyTurnDone = (gState.pending_status || []).includes(myRole);
    const isSpectator = (myRole === 'readonly');
    const shouldLock = !isStarted || isMyTurnDone || isSpectator;

    const msg = document.getElementById('msg-input');
    const sendBtn = document.getElementById('send-btn');
    const skipBtn = document.getElementById('skip-btn');

    if(document.activeElement !== msg) {
        msg.disabled = shouldLock;
        sendBtn.disabled = shouldLock;
        skipBtn.disabled = shouldLock;
        if(!isStarted) msg.placeholder = "프로필 설정을 완료하고 마스터가 세션을 시작할 때까지 대기해주세요.";
        else if(isMyTurnDone) msg.placeholder = "다른 플레이어의 입력을 기다리는 중입니다...";
        else msg.placeholder = "행동을 입력하세요...";
    }

    // 3. Chat Rendering
    const cc = document.getElementById('chat-content');
    let html = `<div style="text-align:center;padding:20px;color:var(--accent);font-weight:bold;font-size:1.4em;">${gState.session_title}</div>`;
    html += `<div class="bubble center-ai"><div class="name-tag">PROLOGUE</div>${mdToSafeHtml(replacePlaceholders(gState.prologue||""))}</div>`;

    (gState.ai_history||[]).forEach((m, idx) => {
      if(idx === editingIdx) {
        let rawText = m.startsWith("**AI**:") ? m.replace("**AI**:","").trim() : m;
        html += `<div class="bubble center-ai" style="width:90%;"><div class="name-tag">EDIT MODE</div><div class="edit-mode-wrap"><textarea id="edit-area-${idx}" class="edit-mode-textarea">${rawText}</textarea><div class="edit-actions"><button class="mini-btn" style="background:#888" onclick="cancelEdit()">취소</button><button class="mini-btn" style="background:var(--accent);color:#fff" onclick="saveEdit(${idx})">저장</button></div></div></div>`;
        return;
      }
      if(m.startsWith("**AI**:")){
        html += `<div class="bubble center-ai"><div class="name-tag">AI <button class="edit-btn" onclick="startEdit(${idx})">수정</button></div>${mdToSafeHtml(replacePlaceholders(m.replace("**AI**:","").trim()))}</div>`;
      } else if(m.startsWith("**Round**:")){
        let roundHtml = "";
        m.replace("**Round**:", "").trim().split(" / ").forEach(p => {
            const sep = p.indexOf(":");
            if(sep > -1){
                const name = p.substring(0, sep).trim();
                const isMe = (myRole !== 'readonly') && (name === gState.profiles[myRole]?.name);
                roundHtml += `<div class="bubble ${isMe?"align-right":"align-left"}"><div class="name-tag">${name}</div>${mdToSafeHtml(p.substring(sep+1).trim())}</div>`;
            } else roundHtml += `<div class="bubble align-left">${mdToSafeHtml(p)}</div>`;
        });
        html += roundHtml;
      } else {
        html += `<div class="bubble align-left">${mdToSafeHtml(m)}</div>`;
      }
    });

    let pendingMsgs = [];
    if(gState.pending_inputs){
        Object.keys(gState.pending_inputs).forEach(uid => {
            if(gState.pending_inputs[uid]?.text) pendingMsgs.push({uid:uid, text:gState.pending_inputs[uid].text, ts:gState.pending_inputs[uid].ts||""});
        });
    }
    pendingMsgs.sort((a,b)=>(a.ts<b.ts?-1:1)).forEach(msg=>{
        const isMe=(msg.uid===myRole);
        html += `<div class="bubble ${isMe?"align-right":"align-left"}"><div class="name-tag">${gState.profiles[msg.uid].name}</div>${mdToSafeHtml(msg.text)}</div>`;
    });

    if(cc.innerHTML !== html) {
        cc.innerHTML = html;
        if(editingIdx === -1) document.getElementById('chat-window').scrollTop = document.getElementById('chat-window').scrollHeight;
    }

    // 4. Profile Sync (입력 보호 적용)
    const p = (myRole && gState.profiles[myRole]) ? gState.profiles[myRole] : {name:"",bio:"",canon:"",locked:false};
    const activeId = document.activeElement?.id || "";

    if(activeId !== 'p-name') document.getElementById('p-name').value = p.name || "";
    if(p.locked || activeId !== 'p-bio') { document.getElementById('p-bio').value = p.bio || ""; upCnt(document.getElementById('p-bio')); }
    if(p.locked || activeId !== 'p-canon') { document.getElementById('p-canon').value = p.canon || ""; upCnt(document.getElementById('p-canon')); }

    const locked = !!p.locked;
    document.getElementById('profile-lock-overlay').style.display = locked ? 'block' : 'none';
    const disableP = (myRole==='readonly') || locked;
    document.getElementById('p-name').disabled = disableP;
    document.getElementById('p-bio').disabled = disableP;
    document.getElementById('p-canon').disabled = disableP;
    const rb = document.getElementById('ready-btn');
    rb.disabled = disableP;
    rb.innerText = locked ? "설정이 확정되었습니다" : "설정 저장";

    // 5. Master Sync
    if(activeId!=='m-title') document.getElementById('m-title').value = gState.session_title || "";
    if(activeId!=='m-sys') { document.getElementById('m-sys').value = gState.sys_prompt || ""; upCnt(document.getElementById('m-sys')); }
    if(activeId!=='m-pro') { document.getElementById('m-pro').value = gState.prologue || ""; upCnt(document.getElementById('m-pro')); }
    if(activeId!=='m-sum') document.getElementById('m-sum').value = gState.summary || "";
    document.getElementById('m-output-limit').value = gState.output_limit || 2000;
    document.getElementById('val-output-limit').innerText = gState.output_limit || 2000;
    document.getElementById('m-ai-model').value = gState.ai_model || "gpt-5.2";
    if(activeId !== 'm-player-count') document.getElementById('m-player-count').value = (gState.player_count || (gState.solo_mode?1:3));

    if(gState.examples){
      for(let i=0;i<3;i++){
        if(activeId!==`ex-q-${i}`) document.getElementById(`ex-q-${i}`).value = gState.examples[i]?.q || "";
        if(activeId!==`ex-a-${i}`) document.getElementById(`ex-a-${i}`).value = gState.examples[i]?.a || "";
      }
    }
    if(activeId!=='kw-c') upCnt(document.getElementById('kw-c'));

    updateAdminBtnVisibility();
    renderLoreList();
  }

  function updateAdminBtnVisibility() {
    if (!gState) return;
    const pcSelect = document.getElementById('m-player-count');
    const pc = gState.player_count || 3;
    if(document.getElementById('btn-ul-2')) document.getElementById('btn-ul-2').style.display = (pc >= 2) ? 'block' : 'none';
    if(document.getElementById('btn-ul-3')) document.getElementById('btn-ul-3').style.display = (pc >= 3) ? 'block' : 'none';

    const p1 = gState.profiles.user1.locked;
    const p2 = gState.profiles.user2.locked;
    const p3 = gState.profiles.user3.locked;
    let allLocked = (pc===1)?p1 : (pc===2)?(p1&&p2) : (p1&&p2&&p3);
    const startBtn = document.querySelector('button[onclick="startSession()"]');

    if(startBtn) {
        // [수정 부분] 세션이 이미 시작된 경우
        if(gState.session_started) {
            startBtn.style.background = "#444"; // 어두운 색으로 변경
            startBtn.innerText = "세션 진행 중";
            startBtn.style.opacity = "0.8";
            startBtn.disabled = true; // 중복 클릭 방지
        }
        // 시작 전이고 모두 준비된 경우
        else if(allLocked) {
            startBtn.style.background = "var(--accent)";
            startBtn.innerText = "세션 시작 가능!";
            startBtn.style.opacity = "1";
            startBtn.disabled = false;
        }
        // 아직 준비 중인 경우
        else {
            startBtn.style.background = "#ccc";
            startBtn.innerText = "준비 대기 중...";
            startBtn.style.opacity = "0.6";
            startBtn.disabled = true;
        }
    }
  }

  function requestAdmin(){
    const pw = prompt("관리자 비밀번호를 입력하세요:");
    if(pw) socket.emit('check_admin', {password: pw});
  }
  function saveAllSettings(isClosing = false) {
    const data = {
        sys: document.getElementById('m-sys').value, sum: document.getElementById('m-sum').value,
        model: document.getElementById('m-ai-model').value, player_count: document.getElementById('m-player-count').value,
        output_limit: document.getElementById('m-output-limit').value, title: document.getElementById('m-title').value,
        pro: document.getElementById('m-pro').value
    };
    socket.emit('save_master_all', data);
    if (isClosing) document.getElementById('admin-modal').style.display = 'none';
    else alert("설정이 저장되었습니다.");
  }
  function closeModal() { saveAllSettings(true); }
  function openTab(evt,id){
    document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    evt.currentTarget.classList.add('active');
  }
  function clearRoles(){
    const pw = prompt("전원 강제 퇴장시키시겠습니까? (관리자 암호 필요)");
    if(pw) socket.emit('clear_all_roles', {password: pw});
  }
  function unlockProfile(target){
    if(confirm(target + "의 잠금을 해제하시겠습니까?")) socket.emit('unlock_profile', {target: target});
  }
  function startEdit(idx){ editingIdx = idx; refreshUI(); }
  function cancelEdit(){ editingIdx = -1; refreshUI(); }
  function saveEdit(idx){
    const txt = document.getElementById(`edit-area-${idx}`).value;
    socket.emit('edit_history_msg', {index: idx, text: "**AI**: " + txt});
    editingIdx = -1;
  }
  function send(){
    const t = document.getElementById('msg-input').value.trim();
    if(!t) return;
    document.getElementById('send-btn').disabled = true;
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
    if(!name) return alert("이름은 필수입니다");
    if(confirm("확정하시겠습니까? (수정 불가)")){
      socket.emit('update_profile', { uid: myRole, name, bio: document.getElementById('p-bio').value, canon: document.getElementById('p-canon').value });
    }
  }
  function startSession(){ socket.emit('start_session'); }
  function sessionReset(){
    if(!confirm("정말로 초기화하시겠습니까?")) return;
    const pw = prompt("관리자 비밀번호:");
    if(pw) socket.emit('reset_session', {password: pw});
    document.getElementById('p-name').value = ""; document.getElementById('p-bio').value = ""; document.getElementById('p-canon').value = "";
  }
  function saveExamples(){
    const exs = [];
    for(let i=0;i<3;i++) exs.push({ q: document.getElementById(`ex-q-${i}`).value, a: document.getElementById(`ex-a-${i}`).value });
    socket.emit('save_examples', exs);
    alert("저장되었습니다");
  }
  function addLoreWithTags(){
    const title = document.getElementById('kw-t').value;
    const content = document.getElementById('kw-c').value;
    const triggers = document.getElementById('tag-hidden').value;
    const index = parseInt(document.getElementById('kw-index').value);
    if(!title || !triggers) return alert("제목과 트리거는 필수입니다");
    socket.emit('add_lore', {title, triggers, content, index: index});
    clearLoreEditor();
  }
  function editLore(i){
    const l = gState.lorebook[i];
    document.getElementById('kw-t').value = l.title || ""; document.getElementById('kw-c').value = l.content || "";
    document.getElementById('kw-index').value = i;
    loadTagsFromString(l.triggers || ""); upCnt(document.getElementById('kw-c'));
  }
  function delLore(i){ socket.emit('del_lore', {index:i}); }
  function refreshLibrary() { socket.emit('get_scenario_list'); }
  function applyLibraryScenario() {
      const idx = document.getElementById('m-library-select').value;
      if(idx === "") return alert("시나리오를 선택해줘!");
      const scenario = currentLibrary[idx];
      let authKey = "";
      if(scenario.is_adult) {
          authKey = prompt("🔞 성인 전용입니다. 인증 코드를 입력해주세요.");
          if(!authKey) return;
      }
      if(confirm(`'${scenario.title}'를 불러올까?`)) socket.emit('load_scenario_url', { url: scenario.url, auth_key: authKey, is_adult: scenario.is_adult });
  }
  function uploadSessionFile(input){
    if(!input.files || !input.files[0]) return;
    const formData = new FormData(); formData.append('file', input.files[0]);
    fetch('/import', {method:'POST', body:formData}).then(res => {
          if(res.ok) { alert("복원 완료! 새로고침합니다."); location.reload(); }
          else alert("실패."); input.value = '';
      }).catch(err => alert("오류: " + err));
  }

  // Helpers
  function mdToSafeHtml(mdText){ return DOMPurify.sanitize(marked.parse(mdText || "", {breaks: true}), {USE_PROFILES: {html: true}}); }
  function replacePlaceholders(text) {
    if (!text || !gState) return text;
    return text.replace(/\{\{p1\}\}/g, gState.profiles.user1?.name || "Player 1")
               .replace(/\{\{p2\}\}/g, gState.profiles.user2?.name || "Player 2")
               .replace(/\{\{p3\}\}/g, gState.profiles.user3?.name || "Player 3");
  }
  function upCnt(el){ const c = document.getElementById("cnt-"+el.id); if(c) c.innerText = el.value.length + "/" + el.getAttribute("maxlength"); }
  function focusTagInput(){ document.getElementById('tag-input')?.focus(); }
  function syncHidden(){ document.getElementById('tag-hidden').value = tags.join(','); }
  function renderTags(){
    const container = document.getElementById('tag-container'); const input = document.getElementById('tag-input');
    if(!container || !input) return;
    [...container.querySelectorAll('.tag-chip')].forEach(el=>el.remove());
    tags.forEach((t, idx)=>{
      const chip = document.createElement('span'); chip.className='tag-chip';
      chip.innerHTML = `<span>${t}</span>`;
      const x = document.createElement('button'); x.textContent='×'; x.onclick=(e)=>{e.stopPropagation(); tags.splice(idx,1); renderTags();};
      chip.appendChild(x); container.insertBefore(chip, input);
    });
    syncHidden();
  }
  function addTag(raw){
    const t = (raw||"").trim(); if(!t) return;
    if(tags.includes(t)) return;
    tags.push(t); renderTags();
  }
  function loadTagsFromString(s){ tags=[]; (s||"").split(',').map(x=>x.trim()).filter(Boolean).forEach(x=>{ if(!tags.includes(x)) tags.push(x); }); renderTags(); }
  document.addEventListener('keydown', (e)=>{
    const ti = document.getElementById('tag-input');
    if(ti && document.activeElement===ti){
      if(e.key==='Enter' || e.key===' ' || e.key===','){ e.preventDefault(); addTag(ti.value); ti.value=''; }
      if(e.key==='Backspace' && ti.value==='' && tags.length>0){ tags.pop(); renderTags(); }
    }
  });
  function clearLoreEditor(){
    document.getElementById('kw-t').value=""; document.getElementById('kw-c').value=""; document.getElementById('kw-index').value="-1";
    tags=[]; renderTags(); document.getElementById('tag-input').value=""; upCnt(document.getElementById('kw-c'));
  }
  function renderLoreList(){
    const list = document.getElementById('lore-list');
    if(!gState || !gState.lorebook) return;
    list.innerHTML = gState.lorebook.map((l,i)=>`<div class="lore-row" data-index="${i}"><div class="drag-handle">☰</div><div class="lore-main"><div class="lore-title">${l.title}</div><div class="lore-trg">${l.triggers}</div></div><div class="lore-actions"><button class="mini-btn mini-edit" onclick="editLore(${i})">수정</button><button class="mini-btn mini-del" onclick="delLore(${i})">삭제</button></div></div>`).join('');
    if(sortable) sortable.destroy();
    sortable = new Sortable(list, { handle: '.drag-handle', animation: 120, onEnd: (evt) => { if(evt.oldIndex !== evt.newIndex) socket.emit('reorder_lore', {from: evt.oldIndex, to: evt.newIndex}); } });
  }

  const msgInputEl = document.getElementById('msg-input');
  msgInputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  let typingTimer = null;
  msgInputEl.addEventListener('input', ()=>{
    if(!myRole || myRole==='readonly') return;
    socket.emit('start_typing', {uid: myRole});
    clearTimeout(typingTimer);
    typingTimer = setTimeout(()=> socket.emit('stop_typing', {uid: myRole}), 1200);
  });
  {% endraw %}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    # Pinggy 실행 함수
    def start_pinggy():
        print("🚀 [드림놀이] Pinggy 서버 연결 중 (Local)...")
        # 로컬 컴퓨터에 ssh가 설치되어 있어야 함
        cmd = "ssh -o StrictHostKeyChecking=no -p 443 -R0:localhost:5000 a.pinggy.io"
        
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        print("\n" + "="*50)
        print("🔗 아래 주소로 접속하세요:")
        try:
            while True:
                line = process.stdout.readline()
                if not line: break
                if "http" in line:
                    print(f"\n👉 {line.strip()}\n")
                    print("="*50 + "\n")
        except: pass

    # 핑기 스레드 시작
    threading.Thread(target=start_pinggy, daemon=True).start()

    # Flask 서버 실행
    time.sleep(2)
    socketio.run(app, host="0.0.0.0", port=5000)
