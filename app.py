import os
import json
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
from pyngrok import ngrok
from dotenv import load_dotenv
import openai

# 1. 환경 변수 로드 (.env 파일)
load_dotenv()

# 2. 설정 값 가져오기
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
NGROK_TOKEN = os.getenv('NGROK_AUTH_TOKEN')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '3896')

client = openai.OpenAI(api_key=OPENAI_API_KEY)
if NGROK_TOKEN:
    ngrok.set_auth_token(NGROK_TOKEN)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# 3. 데이터 저장 경로 설정 (로컬 data 폴더 사용)
SAVE_PATH = 'data'
if not os.path.exists(SAVE_PATH):
    os.makedirs(SAVE_PATH)
DATA_FILE = os.path.join(SAVE_PATH, "save_data.json")

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4)

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return None
    return None

# --- 서버 초기 상태 ---
initial_state = {
    "session_title": "드림놀이",
    "theme": {"bg": "#0d0d0f", "panel": "#1a1a1f", "accent": "#e91e63"},
    "accent_color": "#e91e63",
    "admin_password": ADMIN_PASSWORD,
    "is_locked": False,
    "profiles": {
        "user1": {"name": "Player 1", "bio": "", "canon": ""},
        "user2": {"name": "Player 2", "bio": "", "canon": ""}
    },
    "ai_history": [],
    "summary": "기록된 줄거리가 없습니다.",
    "prologue": "프롤로그를 작성해주세요.",
    "sys_prompt": "마스터 프롬프트",
    "lorebook": [],
    "examples": []
}

saved_state = load_data()
state = saved_state if saved_state else initial_state

# --- 테마 분석 로직 ---
def analyze_theme_color(title, sys_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
            messages=[{
                "role": "system",
                "content": "너는 웹 디자인 전문가야. 세션 설정에 어울리는 테마 색상 3개를 골라줘. 모든 UI 글씨는 검은색이야. JSON 형식: {\"bg\": \"색상\", \"panel\": \"색상\", \"accent\": \"색상\"}"
            }, {
                "role": "user",
                "content": f"제목: {title}\n설정: {sys_prompt}"
            }],
            response_format={ "type": "json_object" }
        )
        palette = json.loads(response.choices[0].message.content)
        return palette
    except:
        return {"bg": "#0d0d0f", "panel": "#1a1a1f", "accent": "#e91e63"}

# --- HTML 템플릿 (보내주신 코드 유지) ---
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>드림놀이</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        :root {
            --bg: {{ theme.bg if theme else '#ffffff' }};
            --panel: {{ theme.panel if theme else '#f1f3f5' }};
            --accent: {{ theme.accent if theme else '#e91e63' }};
            --text: #000000;
        }

        html, body { height: 100%; margin: 0; overflow: hidden; }
        body { font-family: 'Pretendard', sans-serif; display: flex; background: var(--bg); color: #000000 !important; }
        div, p, span, h1, h2, h3, h4, input, textarea, select, button, .bubble { color: #000000 !important; }

        #main { flex: 1; display: flex; flex-direction: column; height: 100vh; border-right: 1px solid rgba(0,0,0,0.05); }
        #chat-window { flex: 1; overflow-y: auto; padding: 30px 10%; display: flex; flex-direction: column; gap: 15px; }
        #sidebar { width: 320px; height: 100vh; background: var(--panel); padding: 20px; box-sizing: border-box; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }

        textarea, input, select {
            background: var(--bg) !important;
            border: 1px solid rgba(0, 0, 0, 0.1) !important;
            border-radius: 10px; padding: 10px; width: 100%; box-sizing: border-box;
            transition: all 0.2s ease; resize: none !important;
        }
        #msg-input { background: var(--panel) !important; border: 1px solid rgba(0, 0, 0, 0.15) !important; height: 80px; }
        textarea:focus, input:focus { outline: none; border-color: var(--accent) !important; box-shadow: 0 0 5px rgba(0,0,0,0.05); }

        .bubble { padding: 15px 20px; border-radius: 15px; max-width: 85%; line-height: 1.6; font-size: 14px; white-space: pre-wrap; background: rgba(0,0,0,0.03); }
        .center-ai { align-self: center; background: var(--panel) !important; border-left: 5px solid var(--accent); width: 100%; max-width: 800px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
        .user-bubble { align-self: flex-end; border-right: 5px solid var(--accent); background: var(--bg); }

        button { cursor: pointer; border: none; border-radius: 8px; background: var(--accent); padding: 10px; font-weight: bold; transition: 0.2s; }
        button:hover { opacity: 0.8; }
        .btn-reset { background: #ff4444 !important; color: #ffffff !important; margin-top: 20px; }

        #admin-modal { display: none; position: fixed; z-index: 9999; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); align-items: center; justify-content: center; }
        .modal-content { width: 90%; max-width: 700px; max-height: 85vh; overflow-y: auto; background: var(--bg) !important; border: 2px solid var(--accent); padding: 25px; border-radius: 12px; }
        .tab-btn { background: #e0e0e0; color: #666; margin-right: 5px; }
        .tab-btn.active { background: var(--accent); color: #000 !important; }
        .tab-content { display: none; margin-top: 15px; flex-direction: column; gap: 10px; }
        .tab-content.active { display: flex; }
    </style>
</head>
<body>
    <div id="main">
        <div id="chat-window"><div id="chat-content"></div></div>
        <div id="input-area" style="padding:20px; background: var(--bg);">
            <div id="status" style="font-size: 12px; margin-bottom: 5px; color: var(--accent); font-weight: bold;">대기 중</div>
            <div style="display:flex; gap:10px;">
                <textarea id="msg-input" placeholder="설정 완료 후 잠금 버튼을 눌러주세요."></textarea>
                <button onclick="send()" style="width:80px;">전송</button>
            </div>
        </div>
    </div>

    <div id="sidebar">
        <h3>🎭 설정</h3>
        <select id="user-role" onchange="refreshUI()">
            <option value="user1">Player 1</option>
            <option value="user2">Player 2</option>
        </select>
        <input type="text" id="p-name" placeholder="이름">
        <textarea id="p-bio" style="height:120px;" placeholder="캐릭터 설정"></textarea>
        <textarea id="p-canon" style="height:80px;" placeholder="드림캐 설정"></textarea>
        <button onclick="saveProfile()">설정 저장</button>
        <button id="lock-btn" onclick="confirmLock()" style="background:var(--accent); color:white !important;">🔒 설정 완료 및 잠금</button>
        <button onclick="requestAdmin()" style="background:#ddd; margin-top:auto;">⚙️ 마스터 설정</button>
    </div>

    <div id="admin-modal">
        <div class="modal-content">
            <div style="display:flex; gap:5px; margin-bottom:15px;">
                <button class="tab-btn active" onclick="openTab(event, 't-base')">시스템</button>
                <button class="tab-btn" onclick="openTab(event, 't-ex')">예시 학습</button>
                <button class="tab-btn" onclick="openTab(event, 't-lore')">키워드북</button>
                <button onclick="closeModal()" style="margin-left:auto; background:#ddd;">닫기</button>
            </div>

            <div id="t-base" class="tab-content active">
                <input type="text" id="m-title" placeholder="세션 제목">
                <textarea id="m-sys" style="height:180px;" placeholder="시스템 프롬프트"></textarea>
                <textarea id="m-pro" style="height:120px;" placeholder="프롤로그"></textarea>
                <textarea id="m-sum" style="height:80px;" placeholder="줄거리 요약"></textarea>
                <button onclick="saveMaster()">설정 및 테마 업데이트</button>
            </div>

            <div id="t-ex" class="tab-content">
                <h4>💡 AI 예시 대화 학습</h4>
                <div id="ex-inputs">
                    <div style="margin-bottom:10px;"><textarea id="ex-q-0" placeholder="예시 질문 1"></textarea><textarea id="ex-a-0" placeholder="AI 답변 예시 1" style="border-left:4px solid var(--accent);"></textarea></div>
                    <div style="margin-bottom:10px;"><textarea id="ex-q-1" placeholder="예시 질문 2"></textarea><textarea id="ex-a-1" placeholder="AI 답변 예시 2" style="border-left:4px solid var(--accent);"></textarea></div>
                    <div style="margin-bottom:10px;"><textarea id="ex-q-2" placeholder="예시 질문 3"></textarea><textarea id="ex-a-2" placeholder="AI 답변 예시 3" style="border-left:4px solid var(--accent);"></textarea></div>
                </div>
                <button onclick="saveExamples()">예시 대화 저장</button>
                <button class="btn-reset" onclick="sessionReset()">⚠️ 세션 종료 및 전체 초기화</button>
            </div>

            <div id="t-lore" class="tab-content">
                <div style="display:grid; grid-template-columns: 1fr 1fr 60px; gap:5px;">
                    <input type="text" id="kw-t" placeholder="키워드">
                    <input type="text" id="kw-tr" placeholder="트리거">
                    <input type="number" id="kw-p" value="0">
                </div>
                <textarea id="kw-c" style="height:100px;" placeholder="상세 내용"></textarea>
                <button onclick="addLore()">키워드 추가 / 수정</button>
                <div id="lore-list" style="margin-top:10px;"></div>
            </div>
        </div>
    </div>
<script>
    const socket = io();
    let gState = null;

    // [수정] 상태 알림 리스너 추가 (AI 응답 중, 저장 완료 등 표시)
    socket.on('status_update', d => {
        const statusEl = document.getElementById('status');
        if(statusEl) {
            statusEl.innerText = d.msg;
            if(d.msg.includes('❌')) statusEl.style.color = 'red';
            else statusEl.style.color = 'var(--accent)';
        }
    });

    socket.on('initial_state', data => {
        gState = data;
        if (data.theme) {
            const root = document.documentElement.style;
            root.setProperty('--bg', data.theme.bg);
            root.setProperty('--panel', data.theme.panel);
            root.setProperty('--accent', data.theme.accent);
        }
        refreshUI();
    });

    function refreshUI() {
        if(!gState) return;
        renderChat(); 
        renderLore(); 
        applyLockUI();

        const role = document.getElementById('user-role').value;
        const p = gState.profiles[role];

        // [수정] 현재 포커스 된 입력창은 덮어쓰지 않음 (타이핑 방해 금지)
        const activeId = document.activeElement.id;
        
        if(activeId !== 'p-name') document.getElementById('p-name').value = p.name || "";
        if(activeId !== 'p-bio') document.getElementById('p-bio').value = p.bio || "";
        if(activeId !== 'p-canon') document.getElementById('p-canon').value = p.canon || "";
        
        if(activeId !== 'm-title') document.getElementById('m-title').value = gState.session_title || "";
        if(activeId !== 'm-sys') document.getElementById('m-sys').value = gState.sys_prompt || "";
        if(activeId !== 'm-pro') document.getElementById('m-pro').value = gState.prologue || "";
        if(activeId !== 'm-sum') document.getElementById('m-sum').value = gState.summary || "";

        for(let i=0; i<3; i++) {
            if(gState.examples && gState.examples[i]) {
                if(activeId !== `ex-q-${i}`) document.getElementById(`ex-q-${i}`).value = gState.examples[i].q || "";
                if(activeId !== `ex-a-${i}`) document.getElementById(`ex-a-${i}`).value = gState.examples[i].a || "";
            }
        }
    }

        function renderChat() {
            let h = `<div style="text-align:center; padding:20px; color:var(--accent); font-weight:bold; font-size:1.4em;">${gState.session_title}</div>`;
            h += `<div class="bubble center-ai"><b>[PROLOGUE]</b><br>${marked.parse(gState.prologue || "")}</div>`;
            gState.ai_history.forEach(msg => {
                const role = document.getElementById('user-role').value;
                const pName = gState.profiles[role].name;
                const isUser = pName && msg.includes(`**${pName}**:`);
                h += `<div class="bubble ${isUser ? 'user-bubble' : 'center-ai'}">${marked.parse(msg)}</div>`;
            });
            document.getElementById('chat-content').innerHTML = h;
            const win = document.getElementById('chat-window');
            win.scrollTop = win.scrollHeight;
        }

        function send() {
            const input = document.getElementById('msg-input');
            const text = input.value.trim();
            if(!text || !gState.is_locked) return;
            socket.emit('client_message', { uid: document.getElementById('user-role').value, text });
            input.value = '';
        }

        function confirmLock() {
            if(confirm("설정을 완료하고 채팅을 시작하시겠습니까?")) {
                socket.emit('lock_settings');
            }
        }

        function requestAdmin() {
            const pw = prompt("관리자 비밀번호:");
            if(pw) socket.emit('check_admin', { password: pw });
        }

        socket.on('admin_auth_res', d => {
            if(d.success) document.getElementById('admin-modal').style.display = 'flex';
            else alert("비밀번호 불일치");
        });

        function saveMaster() {
            socket.emit('save_master_base', {
                title: document.getElementById('m-title').value,
                sys: document.getElementById('m-sys').value,
                pro: document.getElementById('m-pro').value,
                sum: document.getElementById('m-sum').value
            });
            alert("시스템 설정 저장 완료.");
            closeModal();
        }

        function saveExamples() {
            const exs = [];
            for(let i=0; i<3; i++) {
                exs.push({
                    q: document.getElementById(`ex-q-${i}`).value,
                    a: document.getElementById(`ex-a-${i}`).value
                });
            }
            socket.emit('save_examples', exs);
            alert("AI 학습 데이터 저장 완료.");
        }

        function addLore() {
            const title = document.getElementById('kw-t').value;
            if(!title) return alert("키워드명을 입력하세요.");
            socket.emit('add_lore', {
                title: title,
                triggers: document.getElementById('kw-tr').value,
                content: document.getElementById('kw-c').value,
                priority: parseInt(document.getElementById('kw-p').value) || 0
            });
            document.getElementById('kw-t').value = ""; document.getElementById('kw-tr').value = "";
            document.getElementById('kw-c').value = ""; document.getElementById('kw-p').value = "0";
        }

        function editLore(idx) {
            const l = gState.lorebook[idx];
            document.getElementById('kw-t').value = l.title;
            document.getElementById('kw-tr').value = l.triggers || "";
            document.getElementById('kw-c').value = l.content;
            document.getElementById('kw-p').value = l.priority || 0;
            if(confirm("수정 모드: 이 키워드를 삭제하고 입력창으로 불러올까요?")) {
                socket.emit('del_lore', { index: idx });
            }
        }

        function renderLore() {
            const listDiv = document.getElementById('lore-list');
            if(!gState || !gState.lorebook) return;
            listDiv.innerHTML = gState.lorebook.map((l, i) => `
                <div style="padding:8px; background:rgba(0,0,0,0.03); margin-bottom:5px; border-radius:8px; display:flex; justify-content:space-between; align-items:center; border: 1px solid rgba(0,0,0,0.05);">
                    <span onclick="editLore(${i})" style="cursor:pointer; flex:1;"><b>${l.title}</b> <small>(${l.priority})</small></span>
                    <button onclick="socket.emit('del_lore', {index:${i}})" style="padding:2px 8px; font-size:11px; background:#ff4444; color:white !important;">삭제</button>
                </div>`).join('');
        }

        function openTab(evt, id) {
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            evt.currentTarget.classList.add('active');
        }

        function closeModal() { document.getElementById('admin-modal').style.display='none'; }
        function saveProfile() { socket.emit('update_profile', { uid: document.getElementById('user-role').value, name: document.getElementById('p-name').value, bio: document.getElementById('p-bio').value, canon: document.getElementById('p-canon').value }); alert("프로필 저장됨."); }
        function sessionReset() { if(confirm("전체 초기화하시겠습니까?")) { const pw = prompt("관리자 비밀번호:"); if(pw) socket.emit('reset_session', { password: pw }); } }

        document.getElementById('msg-input').addEventListener('keydown', e => { if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
        socket.emit('request_data');
    </script>
</body>
</html>
"""

# --- 소켓 핸들러 ---
@socketio.on('request_data')
def handle_request():
    emit('initial_state', state)

@socketio.on('lock_settings')
def on_lock_settings():
    p1 = state["profiles"].get("user1", {})
    p2 = state["profiles"].get("user2", {})
    if not p1.get("name") or not p2.get("name"):
        emit('status_update', {'msg': '❌ 모든 플레이어의 이름을 입력해야 설정 잠금이 가능합니다.'})
        return
    state["is_locked"] = True
    save_data()
    emit('initial_state', state, broadcast=True)
    emit('status_update', {'msg': '🔒 설정이 잠겼습니다.'})

@socketio.on('client_message')
def on_client_message(data):
    user_text = data.get('text', '').strip()
    uid = data.get('uid')
    if not user_text: return

    sorted_lore = sorted(state.get('lorebook', []), key=lambda x: x.get('priority', 0), reverse=True)
    active_context = []
    for lore in sorted_lore:
        triggers = [t.strip() for t in lore.get('triggers', '').split(',') if t.strip()]
        if any(trigger in user_text for trigger in triggers):
            active_context.append(f"[{lore['title']}]: {lore['content']}")
        if len(active_context) >= 3: break

    lore_prompt = "\n".join(active_context)
    system_instruction = f"{state['sys_prompt']}\n\n[줄거리]: {state['summary']}\n[참고]: {lore_prompt}"
    messages = [{"role": "system", "content": system_instruction}]

    if state.get('examples'):
        for ex in state['examples']:
            if ex.get('q'): messages.append({"role": "user", "content": ex['q']})
            if ex.get('a'): messages.append({"role": "assistant", "content": ex['a']})

    for h in state['ai_history'][-15:]:
        messages.append({"role": "assistant", "content": h})

    current_user_name = state['profiles'].get(uid, {}).get('name', '유저')
    messages.append({"role": "user", "content": f"{current_user_name}: {user_text}"})

    try:
        response = client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.8)
        ai_response = response.choices[0].message.content
        state["ai_history"].append(f"**{current_user_name}**: {user_text}")
        state["ai_history"].append(f"**AI**: {ai_response}")
        if len(state["ai_history"]) > 60: state["ai_history"] = state["ai_history"][-60:]
        save_data()
        emit('initial_state', state, broadcast=True)
        emit('status_update', {'msg': '✅ 응답 완료'})
    except Exception as e:
        emit('status_update', {'msg': f'❌ 에러: {str(e)}'})

@socketio.on('add_lore')
def on_add_lore(data):
    new_entry = {"title": data.get('title'), "triggers": data.get('triggers'), "content": data.get('content'), "priority": int(data.get('priority', 0))}
    state.setdefault("lorebook", []).append(new_entry)
    save_data()
    emit('initial_state', state, broadcast=True)

@socketio.on('del_lore')
def on_del_lore(data):
    idx = data.get('index')
    if "lorebook" in state and 0 <= idx < len(state["lorebook"]):
        state["lorebook"].pop(idx)
        state["lorebook"] = sorted(state["lorebook"], key=lambda x: x.get('priority', 0), reverse=True)
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('save_master_base')
def on_save_master(data):
    state.update({"session_title": data.get('title'), "sys_prompt": data.get('sys'), "prologue": data.get('pro'), "summary": data.get('sum')})
    state['theme'] = analyze_theme_color(state['session_title'], state['sys_prompt'])
    save_data()
    emit('initial_state', state, broadcast=True)

@socketio.on('save_examples')
def on_save_examples(data):
    state["examples"] = data
    save_data()
    emit('initial_state', state, broadcast=True)

@socketio.on('reset_session')
def on_reset_session(data):
    if str(data.get('password')) == str(state.get('admin_password')):
        state.update({"ai_history": [], "lorebook": [], "summary": "초기화됨", "is_locked": False})
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('update_profile')
def on_profile(data):
    uid = data.get('uid')
    if uid in state["profiles"]:
        state["profiles"][uid].update(data)
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('check_admin')
def check_admin(data):
    emit('admin_auth_res', {'success': str(data.get('password')) == str(state.get('admin_password'))})

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, theme=state.get('theme'))

if __name__ == '__main__':
    if NGROK_TOKEN:
        try:
            ngrok.kill()
            public_url = ngrok.connect(5000).public_url
            print(f"🚀 접속 주소: {public_url}")
        except: pass
    socketio.run(app, port=5000)
