# 드림놀이 (Dream-nori)
### 데스크탑 UI만 지원합니다 / **반드시 같은 링크로 두 분이 함께 접속**해 주세요.
### 무조건 최신 버전 사용 권장, 제가 코딩을 못해서 구버전은 작동이 안 될수도 있습니다.

친구 드림복지해주려고 만든 **2인용 AI 세션 시뮬레이터**입니다.  
기본 AI 모델은 **OpenAI GPT-5.2**로 설정되어 있으며, 필요하시면 `app.py`에서 모델명만 변경하시면 됩니다.  
(일부 동작은 Gemini 계열 사용을 전제로 한 부분이 있어 환경에 따라 차이가 날 수 있습니다.)

---
## 2.0.0 업데이트

기존에는 ngrok 방식을 사용하였으나 pinggy 방식으로 바꾸었습니다.

---
## 필요한 것
- **OpenAI API Key (유료)**: https://platform.openai.com/docs/quickstart
- **Gemini API Key (무료 혹은 유료)**: https://aistudio.google.com/app/api-keys
- **Python (무료)**: https://www.python.org/downloads/

---

## 주요 기능(핵심만)
- **2인 동시 접속 세션 진행**: 같은 링크로 2명이 들어오면 Player 1/2로 자동 배정되어 채팅 기반으로 진행됩니다.
- **키워드북(로어북)**: 트리거 키워드를 등록해두면, 대화에 해당 키워드가 등장할 때 AI가 관련 설정을 참고합니다.
- **세션 설정 저장/복원**: 설정을 JSON으로 백업/복원할 수 있어, 세션 세팅을 재사용하기 쉽습니다.

---

## 🚀 시작하기 (로컬 실행)
### 1) 파이썬 설치
- Python 3.10 또는 3.11을 권장합니다.
- 설치 시 **Add Python to PATH**를 반드시 체크해 주세요.

### 2) 다운로드 및 압축 해제
- GitHub에서 **Code → Download ZIP**으로 받은 뒤, 원하는 폴더에 압축을 풀어주세요.

### 3) 라이브러리 설치(최초 1회)
압축을 푼 폴더에서 터미널(Windows는 `cmd`)을 열고 아래를 실행합니다.

```                           
pip install flask flask-socketio python-socketio openai google-generativeai python-dotenv
```                        


### 4) `.env` 파일 생성 및 키 입력(중요)
프로젝트 폴더에 `.env` 파일을 만들고 아래 형식으로 작성해 저장합니다. (`.txt`가 붙지 않게 주의)

```                            
OPENAI_API_KEY=발급받으신 openai api 키
GEMINI_API_KEY=발급받으신 제미나이 api 키
ADMIN_PASSWORD=내비밀번호
```
                        

### 5) 실행    
```
python app.py                       
```
실행 후 콘솔에 표시되는 **`http://xxxx.pinggy.link`** 또는 **`https://...`** 형태의 주소로 접속하시면 됩니다.
**두 분 모두 동일한 주소로 접속**해야 정상적으로 2인 세션이 진행됩니다.

---

## 🔰 설치가 어려우신 경우(코랩 실행)
1) 코랩 열기:  
https://colab.research.google.com/github/sou-venir/Dream-nori/blob/main/%EC%BD%94%EB%9E%A9%EC%9D%84%EC%9C%84%ED%95%9C%EB%93%9C%EB%A6%BC%EB%86%80%EC%9D%B4.ipynb  
2) 왼쪽 **열쇠(🔑)**에서 `OPENAI_API_KEY`, 'GEMINI_API_KEY', 'ADMIN_PASSWORD' 를 발급받으신 것으로 저장하고, **노트북 액세스**를 ON으로 변경합니다.  
3) **런타임 → 모두 실행**을 누릅니다.  
4) 하단에 출력되는 접속 주소로 들어가시면 됩니다.
