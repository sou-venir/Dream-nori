import os
import json
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
from pyngrok import ngrok
from dotenv import load_dotenv
import openai

# 1. 환경 변수 로드
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

# 3. 데이터 저장 경로 설정
SAVE_PATH = 'data'
if not os.path.exists(SAVE_PATH):
    os.makedirs(SAVE_PATH)
DATA_FILE = os.path.join(SAVE_PATH, "save_data.json")

# --- 전역 상태 변수 ---
initial_state = {
    "session_title": "드림놀이",
    "theme": {"bg": "#ffffff", "panel": "#1a1a1f", "accent": "#e91e63"},
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

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return None
    return None

saved_state = load_data()
state = saved_state if saved_state else initial_state

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4)

# --- 테마 분석 로직 (gpt-5.2 -> gpt-4o 수정됨) ---
def analyze_theme_color(title, sys_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o",  # 모델명 수정 완료
            messages=[{
                "role": "system",
                "content": "모든 글씨는 검은색이므로, 배경(bg)과 패널(panel)은 반드시 글씨가 잘 보이는 밝은 파스텔톤이나 밝은 회색 계열로 골라야 해. JSON 형식: {\"bg\": \"색상\", \"panel\": \"색상\", \"accent\": \"색상\"}"
            }, {
                "role": "user",
                "content": f"제목: {title}\n설정: {sys_prompt}"
            }],
            response_format={ "type": "json_object" }
        )
        palette = json.loads(response.choices[0].message.content)
        return palette
    except:
        return {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"}

# --- HTML 템플릿 (JS 함수명 수정됨) ---
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
        #chat-window { flex: 1; overflow-y: auto; padding: 30px 10%; display: flex; flex-direction: column; gap: 15px; scroll-behavior: smooth; }
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

        #admin-modal {
            display: none; position: fixed; z-index: 10000; left: 0; top: 0;
            width: 100vw; height: 100vh; background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(5px); align-items: center; justify-content: center;
        }
        .modal-content {
            width: 95%; max-width: 1200px; height: 85vh; background: #ffffff;
            border-radius: 16px; display: flex; flex-direction: column;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden;
        }
        .modal-header {
            height: 60px; display: flex; justify-content: space-between; align-items: center;
            padding: 0 25px; background: #f8f9fa; border-bottom: 1px solid #eee;
        }
        .tab-group { display: flex; height: 100%; gap: 10px; }
        .tab-btn {
            border: none; background: none; padding: 0 15px; font-size: 14px; font-weight: 600; color: #777;
            cursor: pointer; position: relative; transition: 0.2s;
        }
        .tab-btn.active { color: var(--accent); }
        .tab-btn.active::after {
            content: ""; position: absolute; bottom: 0; left: 0; width: 100%; height: 3px; background: var(--accent);
        }
        .close-btn { width: 32px; height: 32px; border-radius: 50%; border: none; background: #eee; cursor: pointer; font-size: 16px; }
        .modal-body { flex: 1; display: flex; overflow: hidden; }
        .tab-content { display: none; width: 100%; height: 100%; flex-direction: row; }
        .tab-content.active { display: flex; }
        .editor-side { flex: 1.3; padding: 25px; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; border-right: 1px solid #f0f0f0; }
        .list-side { flex: 0.7; padding: 25px; background: #fafafa; display: flex; flex-direction: column; gap: 15px; overflow-y: auto; }
        
        .editor-side label, .list-side label { font-size: 12px; font-weight: 800; color: #999; text-transform: uppercase; }
        .editor-side input, .editor-side select, .editor-side textarea, .list-side textarea {
            width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 12px; font-size: 14px; font-family: inherit; background: #fff !important;
        }
        .editor-side textarea { flex: 1; min-height: 200px; resize: none; }
        .list-side textarea { height: 100%; resize: none; }
        .save-btn { background: var(--accent); color: white !important; padding: 15px; border-radius: 10px; font-weight: bold; cursor: pointer; border: none; margin-top: 5px; }
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
        <textarea id="p-canon" style="height:80px;" placeholder="관계 설정"></textarea>
        
        <button onclick="saveProfile()" id="ready-btn" style="background:var(--accent); color:white !important;">
            ✅ 설정 저장 및 준비 완료
        </button>
        <div id="ready-status" style="font-size:11px; margin-top:5px; color:#666;">대기 중...</div>
        <div style="flex: 1;"></div>
        <button onclick="requestAdmin()" style="background:transparent; color:#999 !important; border: 1px solid #ddd;">⚙️ 마스터 설정 </button>
    </div>

    <div id="admin-modal">
        <div class="modal-content">
            <div class="modal-header">
                <div class="tab-group">
                    <button class="tab-btn active" onclick="openTab(event, 't-base')">⚙️ 엔진</button>
                    <button class="tab-btn" onclick="openTab(event, 't-story')">🎬 서사</button>
                    <button class="tab-btn" onclick="openTab(event, 't-ex')">💡 학습</button>
                    <button class="tab-btn" onclick="openTab(event, 't-lore')">📚 키워드</button>
                </div>
                <button onclick="closeModal()" class="close-btn">✕</button>
            </div>
            <div class="modal-body">
                <div id="t-base" class="tab-content active">
                    <div class="editor-side">
                        <label>AI 모델 선택</label>
                        <select id="m-ai-model">
                            <option value="gpt-4o">OpenAI GPT-4o</option>
                            <option value="gpt-4-turbo">OpenAI GPT-4 Turbo</option>
                        </select>
                        <label>시스템 프롬프트 (AI 지침)</label>
                        <textarea id="m-sys" placeholder="AI에게 줄 지침..."></textarea>
                        <button onclick="saveMaster()" class="save-btn">💾 엔진 설정 저장</button>
                    </div>
                    <div class="list-side">
                        <label>안내</label>
                        <p style="font-size:13px; color:#666;">엔진 모델과 전체적인 AI의 페르소나를 결정합니다.</p>
                        <button class="btn-reset" onclick="sessionReset()" style="margin-top: auto;">⚠️ 세션 완전 초기화</button>
                    </div>
                </div>
                <div id="t-story" class="tab-content">
                    <div class="editor-side">
                        <label>🏷️ 세션 제목</label>
                        <input type="text" id="m-title" placeholder="제목">
                        <label>📌 현재 상황 요약</label>
                        <textarea id="m-sum" style="height:100px; flex:none;" placeholder="지금까지의 핵심 내용..."></textarea>
                        <label>📖 프롤로그</label>
                        <textarea id="m-pro" placeholder="이야기의 시작..."></textarea>
                        <button onclick="saveMaster()" class="save-btn">💾 모든 서사 저장</button>
                    </div>
                    <div class="list-side">
                        <label>💡 서사 팁</label>
                        <p style="font-size:13px; color:#666;">서사는 AI가 이야기의 맥락을 파악하는 데 가장 중요한 정보야.</p>
                    </div>
                </div>
                <div id="t-ex" class="tab-content">
                    <div class="editor-side">
                        <label>💡 학습 데이터 (대화 예시)</label>
                        <textarea id="ex-data" placeholder="[User]: 안녕!&#10;[AI]: 반가워요! (JSON 형태로 처리 권장)"></textarea>
                        <button onclick="saveExamples()" class="save-btn">💡 학습 데이터 저장</button>
                    </div>
                    <div class="list-side"><label>도움말</label><p style="font-size:12px;">원하는 말투를 직접 적어줘.</p></div>
                </div>
                <div id="t-lore" class="tab-content">
                    <div class="editor-side">
                        <label>🔍 키워드 이름</label>
                        <input type="text" id="kw-t" placeholder="이름">
                        <label>🎯 트리거 (쉼표로 구분)</label>
                        <input type="text" id="kw-tr" placeholder="태그1, 태그2...">
                        <label>📝 상세 설정</label>
                        <textarea id="kw-c" placeholder="AI에게 전달할 설정 내용..."></textarea>
                        <input type="number" id="kw-p" value="0" placeholder="우선순위">
                        <button onclick="addLore()" class="save-btn">➕ 키워드 저장</button>
                    </div>
                    <div class="list-side">
                        <label>📋 저장된 키워드</label>
                        <div id="lore-list" style="flex: 1; overflow-y: auto; display:flex; flex-direction:column; gap:8px;"></div>
                    </div>
                </div>
            </div>
        </div>
    </div>

<script>
    const socket = io();
    let gState = null;

    socket.on('status_update', d => {
        const statusEl = document.getElementById('status');
        if(statusEl) {
            statusEl.innerText = d.msg;
            statusEl.style.color = d.msg.includes('❌') ? 'red' : 'var(--accent)';
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

    function typeWriter(element, text, i = 0) {
        if (i === 0) {
            element.innerHTML = "";
            element.style.whiteSpace = "pre-wrap";
        }
        if (i < text.length) {
            element.textContent += text.charAt(i);
            i++;
            const win = document.getElementById('chat-window');
            win.scrollTop = win.scrollHeight;
            setTimeout(() => typeWriter(element, text, i), 10); 
        } else {
            element.innerHTML = marked.parse(text);
            const win = document.getElementById('chat-window');
            win.scrollTop = win.scrollHeight;
        }
    }

    function refreshUI() {
        if(!gState) return;
        renderChat();
        renderLore();
        applyLockUI();
        
        const role = document.getElementById('user-role').value;
        const p = gState.profiles[role];
        const activeId = document.activeElement.id;

        if(activeId !== 'p-name') document.getElementById('p-name').value = p.name || "";
        if(activeId !== 'p-bio') document.getElementById('p-bio').value = p.bio || "";
        if(activeId !== 'p-canon') document.getElementById('p-canon').value = p.canon || "";

        if(activeId !== 'm-title') document.getElementById('m-title').value = gState.session_title || "";
        if(activeId !== 'm-sys') document.getElementById('m-sys').value = gState.sys_prompt || "";
        if(activeId !== 'm-pro') document.getElementById('m-pro').value = gState.prologue || "";
        if(activeId !== 'm-sum') document.getElementById('m-sum').value = gState.summary || "";
    }

    function applyLockUI() {
        if(!gState) return;
        const role = document.getElementById('user-role').value;
        const p = gState.profiles[role];
        const isLocked = (p.name && p.name !== "Player 1" && p.name !== "Player 2" && gState.is_locked); // 조건 완화 혹은 강화 필요시 수정

        // 개별 플레이어 설정 고정 로직
        // 여기서는 간단히 이름이 설정되어 있고 저장 버튼 눌렀으면 잠금 처리 (간소화)
    }

    function renderChat() {
        let h = `<div style="text-align:center; padding:20px; color:var(--accent); font-weight:bold; font-size:1.4em;">${gState.session_title}</div>`;
        h += `<div class="bubble center-ai"><b>[PROLOGUE]</b><br>${marked.parse(gState.prologue || "")}</div>`;

        const contentDiv = document.getElementById('chat-content');
        const history = gState.ai_history;
        const role = document.getElementById('user-role').value;
        const pName = gState.profiles[role].name;

        history.forEach((msg, index) => {
            const isAI = msg.startsWith("**AI**:");
            const isUser = pName && msg.includes(`**${pName}**:`);
            const isLastMsg = (index === history.length - 1);
            
            if (isLastMsg && isAI) {
                const bubbleId = `typing-${index}`;
                h += `<div id="${bubbleId}" class="bubble center-ai"></div>`;
                contentDiv.innerHTML = h;
                const targetElement = document.getElementById(bubbleId);
                // 이미 타이핑 중이면 스킵하는 로직이 필요할 수 있음
                if(!targetElement.hasAttribute('data-typed')) {
                    targetElement.setAttribute('data-typed', 'true');
                    typeWriter(targetElement, msg); 
                }
            } else {
                h += `<div class="bubble ${isUser ? 'user-bubble' : 'center-ai'}">${marked.parse(msg)}</div>`;
            }
        });

        if (history.length === 0 || !history[history.length-1].startsWith("**AI**:")) {
            contentDiv.innerHTML = h;
        }
        const win = document.getElementById('chat-window');
        win.scrollTop = win.scrollHeight;
    }

    function send() {
        const input = document.getElementById('msg-input');
        const text = input.value.trim();
        if(!text) return;
        socket.emit('client_message', { uid: document.getElementById('user-role').value, text });
        input.value = '';
    }

    function requestAdmin() {
        const pw = prompt("관리자 비밀번호:");
        if(pw) socket.emit('check_admin', { password: pw });
    }

    socket.on('admin_auth_res', d => {
        if(d.success) {
            document.getElementById('admin-modal').style.display = 'flex';
            refreshUI();
        } else {
            alert("비밀번호 불일치");
        }
    });

    function saveMaster() {
        const masterData = {
            title: document.getElementById('m-title').value,
            sys: document.getElementById('m-sys').value,
            pro: document.getElementById('m-pro').value,
            sum: document.getElementById('m-sum').value,
            model: document.getElementById('m-ai-model').value
        };
        socket.emit('save_master_base', masterData);
        alert("마스터 설정 저장 완료!");
        closeModal();
    }

    function saveExamples() {
        // 예시 데이터 처리 로직 간소화 (JSON 파싱 등은 필요시 추가)
        const raw = document.getElementById('ex-data').value;
        // 임시로 그냥 raw 텍스트로 보냄 (서버가 리스트 기대하면 수정 필요)
        socket.emit('save_examples', []); 
        alert("학습 데이터 저장 완료 (구현 필요)");
    }

    function addLore() {
        const title = document.getElementById('kw-t').value;
        if(!title) return alert("키워드명을 입력하세요.");
        socket.emit('add_lore', {
            title: title,
            triggers: document.getElementById('kw-tr').value,
            content: document.getElementById('kw-c').value,
            priority: document.getElementById('kw-p').value
        });
        document.getElementById('kw-t').value = ""; 
        document.getElementById('kw-tr').value = "";
        document.getElementById('kw-c').value = "";
    }

    function editLore(idx) {
        const l = gState.lorebook[idx];
        document.getElementById('kw-t').value = l.title;
        document.getElementById('kw-tr').value = l.triggers || "";
        document.getElementById('kw-c').value = l.content;
        document.getElementById('kw-p').value = l.priority || 0;
        if(confirm("수정 모드: 기존 키워드를 삭제하고 입력창으로 불러옵니다.")) {
            socket.emit('del_lore', { index: idx });
        }
    }

    function renderLore() {
        const listDiv = document.getElementById('lore-list');
        if(!gState || !gState.lorebook) return;
        listDiv.innerHTML = gState.lorebook.map((l, i) => `
            <div style="padding:8px; background:rgba(0,0,0,0.03); margin-bottom:5px; border-radius:8px; display:flex; justify-content:space-between; align-items:center; border: 1px solid rgba(0,0,0,0.05);">
                <span onclick="editLore(${i})" style="cursor:pointer; flex:1; font-size:13px;">
                    <b>${l.title}</b> <small style="color:#666;">(P:${l.priority})</small>
                </span>
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

    function saveProfile() {
        const role = document.getElementById('user-role').value;
        const name = document.getElementById('p-name').value;
        if(!name || name.includes("Player")) return alert("이름을 입력하세요!");
        
        if(confirm("설정을 저장하시겠습니까?")) {
            socket.emit('update_profile', {
                uid: role,
                name: name,
                bio: document.getElementById('p-bio').value,
                canon: document.getElementById('p-canon').value
            });
            alert("저장되었습니다.");
        }
    }

    function sessionReset() { 
        if(confirm("초기화하시겠습니까?")) { 
            const pw = prompt("비밀번호:"); 
            if(pw) socket.emit('reset_session', { password: pw }); 
        } 
    }

    document.getElementById('msg-input').addEventListener('keydown', e => { 
        if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send(); } 
    });
    
    socket.emit('request_data');
</script>
</body>
</html>
"""

# --- SocketIO 핸들러 ---

@app.route('/')
def index():
    current_theme = state.get('theme', {"bg": "#ffffff", "panel": "#f1f3f5", "accent": "#e91e63"})
    return render_template_string(HTML_TEMPLATE, theme=current_theme)

@socketio.on('request_data')
def handle_request():
    emit('initial_state', state)

@socketio.on('lock_settings')
def on_lock_settings():
    p1 = state["profiles"].get("user1", {})
    p2 = state["profiles"].get("user2", {})
    if not p1.get("name") or not p2.get("name"):
        emit('status_update', {'msg': '❌ 모든 플레이어 이름을 입력해야 합니다.'})
        return
    state["is_locked"] = True
    save_data()
    emit('initial_state', state, broadcast=True)
    emit('status_update', {'msg': '🔒 설정 잠금 완료'})

@socketio.on('client_message')
def on_client_message(data):
    user_text = data.get('text', '').strip()
    uid = data.get('uid')
    if not user_text: return

    # 키워드(Lorebook) 매칭
    sorted_lore = sorted(state.get('lorebook', []), key=lambda x: int(x.get('priority', 0)), reverse=True)
    active_context = []
    for lore in sorted_lore:
        triggers = [t.strip() for t in lore.get('triggers', '').split(',') if t.strip()]
        if any(trigger in user_text for trigger in triggers):
            active_context.append(f"[{lore['title']}]: {lore['content']}")
        if len(active_context) >= 3: break

    lore_prompt = "\n".join(active_context)
    
    system_instruction = f"{state['sys_prompt']}\n\n[줄거리]: {state['summary']}\n[참고]: {lore_prompt}"
    messages = [{"role": "system", "content": system_instruction}]

    # 예시 추가
    if state.get('examples'):
        # examples 구조에 따라 유연하게 처리 필요 (현재는 빈 리스트일 수 있음)
        pass 

    # 히스토리 추가
    for h in state['ai_history'][-15:]:
        # 히스토리 포맷 파싱 (간단히 처리)
        if h.startswith("**AI**:"):
            messages.append({"role": "assistant", "content": h.replace("**AI**: ", "")})
        else:
            # 유저 이름 파싱 로직이 필요하지만 간단히
            content = h.split(": ", 1)[-1] if ": " in h else h
            messages.append({"role": "user", "content": content})

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
    new_entry = {
        "title": data.get('title'),
        "triggers": data.get('triggers'),
        "content": data.get('content'),
        "priority": int(data.get('priority', 0))
    }
    state.setdefault("lorebook", []).append(new_entry)
    save_data()
    emit('initial_state', state, broadcast=True)

@socketio.on('del_lore')
def on_del_lore(data):
    idx = data.get('index')
    if "lorebook" in state and 0 <= idx < len(state["lorebook"]):
        state["lorebook"].pop(idx)
        state["lorebook"].sort(key=lambda x: int(x.get('priority', 0)), reverse=True)
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('save_master_base')
def on_save_master(data):
    state.update({
        "session_title": data.get('title'),
        "sys_prompt": data.get('sys'),
        "prologue": data.get('pro'),
        "summary": data.get('sum')
    })
    # 테마 자동 분석
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
        state.update({
            "ai_history": [],
            "lorebook": [],
            "summary": "초기화됨",
            "is_locked": False,
            "session_title": "새로운 세션"
        })
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('update_profile')
def on_profile(data):
    uid = data.get('uid')
    if uid in state["profiles"]:
        state["profiles"][uid].update({
            "name": data.get('name'),
            "bio": data.get('bio'),
            "canon": data.get('canon')
        })
        save_data()
        emit('initial_state', state, broadcast=True)

@socketio.on('check_admin')
def check_admin(data):
    success = str(data.get('password')) == str(state.get('admin_password'))
    emit('admin_auth_res', {'success': success})

if __name__ == '__main__':
    try:
        ngrok.kill()
        public_url = ngrok.connect(5000).public_url
        print("\n" + "="*50)
        print(f"🚀 드림 시뮬레이터 서버 실행 중!")
        print(f"🔗 접속 주소: {public_url}")
        print("="*50 + "\n")
        socketio.run(app, port=5000, allow_unsafe_werkzeug=True)
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
