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

saved_state = load_data()
state = saved_state if saved_state else initial_state

# --- 테마 분석 로직 ---
def analyze_theme_color(title, sys_prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-5.2",
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
        return {"bg": "#0d0d0f", "panel": "#1a1a1f", "accent": "#e91e63"}

# --- HTML 템플릿 (보내주신 코드 유지) ---
HTML_TEMPLATE = """<!DOCTYPE html>

<html>

<head>

    <meta charset=\"UTF-8\">

    <title>드림놀이</title>

    <script src=\"https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js\"></script>

    <script src=\"https://cdn.jsdelivr.net/npm/marked/marked.min.js\"></script>

    <style>

        :root {

            --bg: {{ theme.bg if theme else '#ffffff' }};

            --panel: {{ theme.panel if theme else '#f1f3f5' }};

            --accent: {{ theme.accent if theme else '#e91e63' }};

            --text: #000000;

        }


        /* 1. 모달이 화면을 벗어나지 않게 고정 */

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



        /* 1. 모달 배경 및 컨테이너 */
#admin-modal {
    display: none;
    position: fixed;
    z-index: 10000;
    left: 0; top: 0;
    width: 100vw; height: 100vh;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(5px);
    align-items: center; justify-content: center;
}

.modal-content {
    width: 95%; max-width: 1200px; height: 85vh;
    background: #ffffff; border-radius: 16px;
    display: flex; flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    overflow: hidden;
}

/* 2. 상단 헤더 & 탭 메뉴 */
.modal-header {
    height: 60px; display: flex; justify-content: space-between; align-items: center;
    padding: 0 25px; background: #f8f9fa; border-bottom: 1px solid #eee;
}

.tab-group { display: flex; height: 100%; gap: 10px; }
.tab-btn {
    border: none; background: none; padding: 0 15px;
    font-size: 14px; font-weight: 600; color: #777;
    cursor: pointer; position: relative; transition: 0.2s;
}
.tab-btn.active { color: var(--accent); }
.tab-btn.active::after {
    content: ""; position: absolute; bottom: 0; left: 0;
    width: 100%; height: 3px; background: var(--accent);
}

.close-btn {
    width: 32px; height: 32px; border-radius: 50%; border: none;
    background: #eee; cursor: pointer; font-size: 16px;
}

/* 3. 모달 바디 (좌우 분할) */
.modal-body { flex: 1; display: flex; overflow: hidden; }

.tab-content {
    display: none; width: 100%; height: 100%;
    flex-direction: row; /* 좌우 배치 */
}
.tab-content.active { display: flex; }

/* 왼쪽 편집창 */
.editor-side {
    flex: 1.3; padding: 25px; display: flex; flex-direction: column;
    gap: 15px; overflow-y: auto; border-right: 1px solid #f0f0f0;
}

/* 오른쪽 정보창 */
.list-side {
    flex: 0.7; padding: 25px; background: #fafafa;
    display: flex; flex-direction: column; gap: 15px; overflow-y: auto;
}

/* 4. 내부 요소 디자인 */
.editor-side label, .list-side label {
    font-size: 12px; font-weight: 800; color: #999; text-transform: uppercase;
}

.editor-side input, .editor-side select, .editor-side textarea, .list-side textarea {
    width: 100%; border: 1px solid #ddd; border-radius: 8px;
    padding: 12px; font-size: 14px; font-family: inherit;
    background: #fff !important;
}

.editor-side textarea { flex: 1; min-height: 200px; resize: none; }
.list-side textarea { height: 100%; resize: none; }

.save-btn {
    background: var(--accent); color: white !important;
    padding: 15px; border-radius: 10px; font-weight: bold;
    cursor: pointer; border: none; margin-top: 5px;
}

/* 키워드 아이템 */
.lore-item {
    background: #fff; border: 1px solid #eee; padding: 12px;
    border-radius: 10px; position: relative; margin-bottom: 8px;
}

    </style>

</head>

<body>

    <div id=\"main\">

        <div id=\"chat-window\"><div id=\"chat-content\"></div></div>

        <div id=\"input-area\" style=\"padding:20px; background: var(--bg);\">

            <div id=\"status\" style=\"font-size: 12px; margin-bottom: 5px; color: var(--accent); font-weight: bold;\">대기 중</div>

            <div style=\"display:flex; gap:10px;\">

                <textarea id=\"msg-input\" placeholder=\"설정 완료 후 잠금 버튼을 눌러주세요.\"></textarea>

                <button onclick=\"send()\" style=\"width:80px;\">전송</button>

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

    <div id="ready-status" style="font-size:11px; margin-top:5px; color:#666;">
        대기 중...
    </div>

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
                            <option value="gpt-5.2">OpenAI GPT-5.2</option>
                            <option value="gpt-4o">OpenAI GPT-4o</option>
                            <option value="gemini-3-pro-preview">Google Gemini 3 Pro</option>
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
                        <textarea id="ex-data" placeholder="[User]: 안녕!&#10;[AI]: 반가워요!"></textarea>
                        <button onclick="saveExamples()" class="save-btn">💡 학습 데이터 저장</button>
                    </div>
                    <div class="list-side"><label>도움말</label><p style="font-size:12px;">원하는 말투를 직접 적어줘.</p></div>
                </div>

                <div id="t-lore" class="tab-content">
                    <div class="editor-side">
                        <label>🔍 키워드 이름</label>
                        <input type="text" id="kw-t" placeholder="이름">
                        <label>🎯 트리거 (단어 입력 후 엔터/스페이스)</label>
                        <div id="tag-container">
                            <input type="text" id="tag-input" placeholder="태그 추가..." style="border:none !important; width: 100px !important; outline:none; background:transparent !important;">
                        </div>
                        <label>📝 상세 설정</label>
                        <textarea id="kw-c" placeholder="AI에게 전달할 설정 내용..."></textarea>
                        <button id="lore-save-btn" onclick="addLoreWithTags()" class="save-btn">➕ 키워드 저장</button>
                    </div>
                    <div class="list-side">
                        <label>📋 우선순위 (드래그하여 이동)</label>
                        <div id="lore-list" style="flex: 1; overflow-y: auto; display:flex; flex-direction:column; gap:8px;"></div>
                    </div>
                </div>
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

    // 타이핑 효과 함수
function typeWriter(element, text, i = 0) {
    if (i === 0) {
        element.innerHTML = ""; // 처음 시작할 때 비우기
        element.style.whiteSpace = "pre-wrap"; // 줄바꿈 유지
    }

    if (i < text.length) {
        // 텍스트를 한 글자씩 추가 (마크다운 적용 전 raw 텍스트로)
        element.textContent += text.charAt(i);
        i++;

        // 스크롤 아래로 고정
        const win = document.getElementById('chat-window');
        win.scrollTop = win.scrollHeight;

        setTimeout(() => typeWriter(element, text, i), 35); // 35ms 속도로 출력
    } else {
        // 타이핑이 모두 끝나면 최종적으로 마크다운 렌더링 적용
        element.innerHTML = marked.parse(text);
    }
}

    function refreshUI() {

        if(!gState) return;

        renderChat();

        renderLore();

        applyLockUI();

    function applyLockUI() {
    if(!gState) return;

    const role = document.getElementById('user-role').value;
    const p = gState.profiles[role];

    // 이미 이름이 저장되어 있는 상태라면 (즉, 한번 확정했다면)
    if(p.name && p.name !== "Player 1" && p.name !== "Player 2") {
        document.getElementById('p-name').readOnly = true;
        document.getElementById('p-bio').readOnly = true;
        document.getElementById('p-canon').readOnly = true;
        document.getElementById('ready-btn').disabled = true;
        document.getElementById('ready-btn').innerText = "🔒 설정 고정됨";
    } else {
        // 아직 설정 전이라면 풀어주기
        document.getElementById('p-name').readOnly = false;
        document.getElementById('p-bio').readOnly = false;
        document.getElementById('p-canon').readOnly = false;
        document.getElementById('ready-btn').disabled = false;
        document.getElementById('ready-btn').innerText = "✅ 설정 저장 및 준비 완료";
    }
}

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

    const contentDiv = document.getElementById('chat-content');
    const history = gState.ai_history;
    const role = document.getElementById('user-role').value;
    const pName = gState.profiles[role].name;

    // 전체 히스토리 렌더링
    history.forEach((msg, index) => {
        const isUser = pName && msg.includes(`**${pName}**:`);
        const isLastMsg = (index === history.length - 1);
        const isAI = msg.startsWith("**AI**:");

        // 마지막 메시지가 AI인 경우에만 타이핑 효과 적용
        if (isLastMsg && isAI) {
            const bubbleId = `typing-${index}`;
            h += `<div id="${bubbleId}" class="bubble center-ai"></div>`;
            contentDiv.innerHTML = h; // 먼저 틀을 만들고

            const targetElement = document.getElementById(bubbleId);
            typeWriter(targetElement, msg); // 타이핑 시작!
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
    // gState.is_locked 체크를 없애거나, 저장 시 True가 되게 해야 함
    if(!text) return;
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
    if(d.success) {
        // 1. 모달 띄우기
        const modal = document.getElementById('admin-modal');
        modal.style.display = 'flex';

        // 2. 모든 탭 숨기기 및 버튼 비활성화 (초기화)
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

        // 3. 첫 번째 탭(시스템)만 강제로 켜기
        document.getElementById('t-base').classList.add('active');
        document.querySelector('.tab-btn').classList.add('active');

        refreshUI(); // 저장된 데이터 다시 불러와서 칸 채우기
    } else {
        alert("비밀번호 불일치");
    }
});



        function saveMaster() {
    // 마스터 창에 있는 모든 입력값을 긁어모아!
    const masterData = {
        title: document.getElementById('m-title').value,
        sys: document.getElementById('m-sys').value,
        pro: document.getElementById('m-pro').value,
        sum: document.getElementById('m-sum').value,
        model: document.getElementById('m-ai-model').value // 마스터 창의 엔진 선택값
    };

    socket.emit('save_master_base', masterData);
    alert("마스터 설정이 모두 저장되었어! 엔진이 " + masterData.model + "(으)로 교체됐어.");
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
            <span onclick="editLore(${i})" style="cursor:pointer; flex:1; font-size:13px;">
                <b>${l.title}</b> <small style="color:#666;">(우선순위: ${l.priority})</small>
            </span>

            <div style="display:flex; gap:3px;">
                <button onclick="editLore(${i})" style="padding:2px 8px; font-size:11px; background:#44aaff; color:white !important;">수정</button>
                <button onclick="socket.emit('del_lore', {index:${i}})" style="padding:2px 8px; font-size:11px; background:#ff4444; color:white !important;">삭제</button>
            </div>
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

    if(!name || name.includes("Player")) {
        return alert("캐릭터 이름을 먼저 입력해주세요!");
    }

    // [핵심] "못 바꿉니다" 경고창
    const logic = `⚠️ 주의: 지금 설정한 내용으로 확정됩니다.\n세션이 시작된 후에는 내용을 수정할 수 없습니다.\n\n정말로 저장하시겠습니까?`;

    if(confirm(logic)) {
        const data = {
            uid: role,
            name: name,
            bio: document.getElementById('p-bio').value,
            canon: document.getElementById('p-canon').value
        };

        socket.emit('update_profile', data);

        // 저장 후 입력창들 잠그기 (AI 혼란 방지)
        document.getElementById('p-name').readOnly = true;
        document.getElementById('p-bio').readOnly = true;
        document.getElementById('p-canon').readOnly = true;
        document.getElementById('ready-btn').disabled = true;
        document.getElementById('ready-btn').innerText = "🔒 설정 고정됨";

        alert("설정이 고정되었습니다. 상대방의 준비를 기다립니다.");
    }
}

        function sessionReset() { if(confirm("전체 초기화하시겠습니까?")) { const pw = prompt("관리자 비밀번호:"); if(pw) socket.emit('reset_session', { password: pw }); } }



        document.getElementById('msg-input').addEventListener('keydown', e => { if(e.key==='Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

        socket.emit('request_data');

    </script>

</body>

</html>
"""

# --- 7. 소켓 핸들러 (저장 로직 추가됨) ---
#플레이어
@socketio.on('lock_settings')
def on_lock_settings():
    # 1. 플레이어 이름이 비어있는지 검사
    p1 = state["profiles"].get("user1", {})
    p2 = state["profiles"].get("user2", {})

    if not p1.get("name") or not p2.get("name"):
        emit('status_update', {'msg': '❌ 모든 플레이어의 이름을 입력해야 설정 잠금이 가능합니다.'})
        return

    # 2. 잠금 상태 업데이트
    state["is_locked"] = True
    save_data()

    # 3. 모든 접속자에게 상태 전송 (이제 화면이 바뀜)
    emit('initial_state', state, broadcast=True)
    emit('status_update', {'msg': '🔒 설정이 잠겼습니다. 이제 수정할 수 없으며 대화가 가능합니다.'})

#뭔진모르겠는데 이거넣으래
@socketio.on('request_data')
def handle_request():
    emit('initial_state', state)

#키워드북 필터링 로직
@socketio.on('client_message')
def on_client_message(data):
    user_text = data.get('text', '').strip()
    uid = data.get('uid')
    if not user_text: return

    # 1. 키워드 필터링
    sorted_lore = sorted(state.get('lorebook', []), key=lambda x: x['priority'], reverse=True)
    active_context = []
    for lore in sorted_lore:
        triggers = [t.strip() for t in lore.get('triggers', '').split(',') if t.strip()]
        if any(trigger in user_text for trigger in triggers):
            active_context.append(f"[{lore['title']}]: {lore['content']}")
        if len(active_context) >= 3: break

    lore_prompt = "\n".join(active_context)

    # 2. AI에게 보낼 메시지 조립
    system_instruction = f"{state['sys_prompt']}\n\n[줄거리]: {state['summary']}\n[참고]: {lore_prompt}"
    messages = [{"role": "system", "content": system_instruction}]

    # 예시(Few-shot) 추가
    if state.get('examples'):
        for ex in state['examples']:
            messages.append({"role": "user", "content": ex['q']})
            messages.append({"role": "assistant", "content": ex['a']})

    # 최근 히스토리 15개 추가
    for h in state['ai_history'][-15:]:
        messages.append({"role": "assistant", "content": h})

    current_user_name = state['profiles'].get(uid, {}).get('name', '유저')
    messages.append({"role": "user", "content": f"{current_user_name}: {user_text}"})

    try:
        response = client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.8)
        ai_response = response.choices[0].message.content

        # [수정포인트] 유저 말과 AI 말을 둘 다 기록에 추가
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
    # 키워드 데이터 구성
    new_entry = {
        "title": data.get('title', '제목 없음'),
        "triggers": data.get('triggers', ''),
        "content": data.get('content', ''),
        "priority": int(data.get('priority', 0))
    }

    # state의 lorebook 리스트에 추가
    if "lorebook" not in state:
        state["lorebook"] = []

    state["lorebook"].append(new_entry)
    save_data() # 변경사항 파일 저장

    # 모든 접속자에게 실시간으로 리스트 갱신 알림
    emit('initial_state', state, broadcast=True)
    print(f"📖 키워드 추가됨: {new_entry['title']}")

@socketio.on('del_lore')
def on_del_lore(data):
    idx = data.get('index')
    if "lorebook" in state and 0 <= idx < len(state["lorebook"]):
        # 1. 일단 해당 항목을 지웁니다.
        removed = state["lorebook"].pop(idx)

        # 2. [추가된 로직] 지운 후 남은 키워드들을 다시 우선순위 높은 순으로 정렬합니다.
        # 이렇게 해야 서버와 화면의 순서가 항상 똑같이 유지됩니다.
        state["lorebook"] = sorted(state["lorebook"], key=lambda x: x.get('priority', 0), reverse=True)

        save_data()
        emit('initial_state', state, broadcast=True)
        print(f"🗑️ 키워드 삭제됨: {removed['title']}")

@socketio.on('save_master_base') # 하나로 통합!
def on_save_master(data):
    # 텍스트 정보 업데이트
    state["session_title"] = data.get('title', state['session_title'])
    state["sys_prompt"] = data.get('sys', state['sys_prompt'])
    state["prologue"] = data.get('pro', state['prologue'])
    state["summary"] = data.get('sum', state['summary'])

    # AI 색상 분석 실행
    print("🎨 AI가 분위기에 어울리는 테마를 생성 중...")
    new_palette = analyze_theme_color(state['session_title'], state['sys_prompt'])
    state['theme'] = new_palette
    state['accent_color'] = new_palette['accent']

    save_data()
    # 모든 접속자에게 변경된 상태 브로드캐스트
    emit('initial_state', state, broadcast=True)
    emit('status_update', {'msg': '✅ 마스터 설정과 테마가 업데이트되었습니다!'}, broadcast=True)

#예시 저장
@socketio.on('save_examples')
def on_save_examples(data):
    state["examples"] = data  # 프론트에서 보낸 [{q:..., a:...}, ...] 리스트 저장
    save_data()
    emit('initial_state', state, broadcast=True)
    print("🧠 AI 학습 예시 데이터가 업데이트되었습니다.")


#세션 전체 초기화
@socketio.on('reset_session')
def on_reset_session(data):
    input_pw = str(data.get('password'))
    if input_pw == str(state.get('admin_password', '3896')):
        # 전체 초기화
        state["ai_history"] = []
        state["lorebook"] = []
        state["summary"] = "기록된 줄거리가 없습니다."
        state["session_title"] = "새로운 세션"
        state["theme"] = {"bg": "#0d0d0f", "panel": "#1a1a1f", "accent": "#e91e63"}

        save_data()
        emit('initial_state', state, broadcast=True)
        emit('status_update', {'msg': '🔄 세션이 전체 초기화되었습니다.'})

@socketio.on('update_profile')
def on_profile(data):
    uid = data.get('uid')
    if uid in state["profiles"]:
        state["profiles"][uid]["name"] = data.get('name', state["profiles"][uid]["name"])
        state["profiles"][uid]["bio"] = data.get('bio', state["profiles"][uid]["bio"])
        state["profiles"][uid]["canon"] = data.get('canon', state["profiles"][uid]["canon"])
        save_data()
        emit('initial_state', state, broadcast=True)
@socketio.on('check_admin')
def check_admin(data):
    # 입력받은 값과 저장된 값을 모두 문자열로 변환하여 비교
    input_pw = str(data.get('password'))
    stored_pw = str(state.get('admin_password', '3896'))

    success = (input_pw == stored_pw)
    emit('admin_auth_res', {'success': success})
        # --- 6. Flask 경로 설정 (이 부분이 있어야 404가 안 뜹니다) ---
@app.route('/')
def index():
    # state에 테마 데이터가 없으면 기본값 사용
    current_theme = state.get('theme', {"bg": "#0d0d0f", "panel": "#1a1a1f", "accent": "#e91e63"})
    return render_template_string(HTML_TEMPLATE, theme=current_theme)

# --- 7. 서버 실행부 (수정됨) ---
if __name__ == '__main__':
    try:
        # 1. 기존 ngrok 터널 초기화
        ngrok.kill()

        # 2. ngrok 터널을 먼저 생성 (서버 실행 전)
        public_url = ngrok.connect(5000).public_url

        print("\n" + "="*50)
        print(f"🚀 드림 시뮬레이터 서버 실행 중!")
        print(f"🔗 접속 주소: {public_url}")
        print(f"🔐 마스터 암호: {state.get('admin_password', '3896')}")
        print("="*50 + "\n")

        # 3. Flask-SocketIO 실행 (이 코드가 마지막에 와야 합니다)
        socketio.run(app, port=5000, allow_unsafe_werkzeug=True)

    except Exception as e:
        print(f"❌ 서버 실행 중 오류 발생: {e}")

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
