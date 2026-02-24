"""
CJM Builder · Flask Backend
SKT Customer Journey Map 자동화 생성 서버

보안 설계:
  - OPENAI_API_KEY  : 서버 환경변수에만 존재. 브라우저에 절대 노출 안 됨.
  - SITE_PASSWORD   : 사이트 접근 비밀번호. 서버에서 검증.
  - SESSION_SECRET  : 세션 쿠키 서명 키. 쿠키 위변조 방지.
  - API 키는 .env 파일 또는 환경변수로만 관리.
"""

import os
import json
import re
import secrets
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, session

app = Flask(__name__)
BASE_DIR = Path(__file__).parent

# ─── .env 파일 자동 로드 (있을 경우) ──────────────────────────
def _load_dotenv():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            # 이미 환경변수에 있으면 덮어쓰지 않음 (Railway 등 배포 환경 우선)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_dotenv()

# ─── 환경변수 ──────────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SITE_PASSWORD  = os.environ.get("SITE_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
OPENAI_MODEL   = os.environ.get("OPENAI_MODEL", "gpt-4o")

app.secret_key = SESSION_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JS에서 쿠키 접근 불가
    SESSION_COOKIE_SAMESITE="Lax", # CSRF 방어
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")),  # Railway HTTPS 자동 감지
    PERMANENT_SESSION_LIFETIME=86400,  # 24시간
)

# ─── Knowledge 캐시 ────────────────────────────────────────────
_knowledge_cache = None


def extract_docx_text(filepath, max_chars=25000):
    try:
        from docx import Document
        doc = Document(str(filepath))
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        table_rows = []
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    table_rows.append(" | ".join(cells))
        full = "\n".join(paragraphs)
        if table_rows:
            full += "\n\n[표]\n" + "\n".join(table_rows)
        return full[:max_chars]
    except Exception as e:
        return f"[읽기 오류: {e}]"


def extract_xlsx_text(filepath, max_chars=15000):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(filepath), data_only=True)
        parts = []
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            parts.append(f"[시트: {sheet}]")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 500:
                    parts.append("... (이하 생략)")
                    break
                cells = [str(c) if c is not None else "" for c in row]
                line = " | ".join(cells).strip()
                if line.replace("|", "").strip():
                    parts.append(line)
        return "\n".join(parts)[:max_chars]
    except Exception as e:
        return f"[읽기 오류: {e}]"


def load_knowledge():
    global _knowledge_cache
    if _knowledge_cache is not None:
        return _knowledge_cache

    print("\n📚 Knowledge 파일 로딩 중...")
    parts = []
    files = sorted([
        f for f in BASE_DIR.iterdir()
        if f.suffix in (".docx", ".xlsx") and not f.name.startswith("~")
    ])
    for fp in files:
        print(f"  📄 {fp.name}")
        text = extract_docx_text(fp) if fp.suffix == ".docx" else extract_xlsx_text(fp)
        parts.append(f"\n{'─'*60}\n📌 파일명: {fp.name}\n{'─'*60}\n{text}\n")

    _knowledge_cache = "\n".join(parts)
    kb = len(_knowledge_cache.encode("utf-8")) / 1024
    print(f"✅ 로딩 완료 ({kb:.0f} KB, {len(files)}개 파일)\n")
    return _knowledge_cache


# ─── System Prompt ─────────────────────────────────────────────
def build_system_prompt(knowledge: str) -> str:
    return f"""당신은 통신 서비스에 특화된 'CJM(Customer Journey Map) 자동화 멀티 에이전트 빌더'입니다.
사용자가 #UserInput을 입력하면 에이전트 1→2→3→4 순서로 실행하여 CJM JSON을 생성합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 1: 쿼리 확장]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
역할: #UserInput을 통신사 서비스 관점의 여정 쿼리로 확장합니다.
포맷: {{#TargetSegment}}의 {{#Channel}}에서 {{#Action}} 여정을 만드세요
최대 5개 쿼리 확장 (모호한 입력은 복수 확장)

채널 정의:
- Tworld: 통신 관리 (요금제, 번호이동, 납부, 데이터 등)
- T멤버십: 혜택 서비스 (할인, 쿠폰, 적립)
- T우주: 구독 서비스
- T다이렉트샵: 단말 구매·개통 커머스
- 고객센터: 콜센터 CS
- 대리점: 오프라인 매장

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 2: Journey 설계]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
역할: 확장된 쿼리 기반으로 실제 사용자 행동 흐름을 6~8단계로 설계합니다.
각 단계는 phase(구간)로 그룹화합니다 (예: 인지→탐색→결정→실행→완료).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 3: Knowledge 기반 CJM 작성 ⭐ 핵심]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
역할: 아래 첨부된 Knowledge(인터뷰/CSI 데이터)를 기반으로 각 단계별 CJM을 작성합니다.
- 실제 인터뷰 데이터를 직접 인용하듯 구체적으로 작성
- user_action: 유저가 취하는 구체적인 행동
- feeling: 감정 상태 (큰따옴표로 직접 인용 형식)
- painpoint: 겪는 어려움/불편함
- needs: 유저가 바라는 점 (숨은 Needs 포함)
- insight: 서비스 기회/솔루션
- source에 반드시 파일명 명시

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 4: 검색/일반지식 기반 CJM 보완]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
역할: UX 관점과 일반 통신 서비스 지식으로 에이전트3이 놓친 핵심 요소를 추가합니다.
특히 잠재적 Needs 발굴에 집중합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[첨부된 데이터 - Knowledge]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{knowledge}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[출력 규칙 - 반드시 JSON으로 출력]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
반드시 아래 JSON 구조로만 출력하세요.

{{
  "cjm_list": [
    {{
      "query": "확장된 쿼리 전체 텍스트",
      "segment": "타겟 세그먼트",
      "channel": "채널명",
      "action": "액션명",
      "steps": [
        {{"num": 1, "name": "단계명", "phase": "구간명"}},
        {{"num": 2, "name": "단계명", "phase": "구간명"}}
      ],
      "table": {{
        "1": {{
          "user_action": {{
            "knowledge": [{{"text": "구체적 행동 서술", "source": "파일명.docx"}}],
            "search": [{{"text": "구체적 행동 서술"}}]
          }},
          "feeling": {{
            "knowledge": [{{"text": "\\"인용구 형식의 감정\\"", "source": "파일명.docx"}}],
            "search": [{{"text": "\\"인용구 형식의 감정\\""}}]
          }},
          "painpoint": {{
            "knowledge": [{{"text": "불편함 서술", "source": "파일명.docx"}}],
            "search": [{{"text": "불편함 서술"}}]
          }},
          "needs": {{
            "knowledge": [{{"text": "요구사항 서술", "source": "파일명.docx"}}],
            "search": [{{"text": "요구사항 서술"}}]
          }},
          "insight": {{
            "knowledge": [{{"text": "솔루션 제안", "source": "파일명.docx"}}],
            "search": [{{"text": "솔루션 제안"}}]
          }}
        }}
      }}
    }}
  ]
}}"""


# ─── Auth 헬퍼 ────────────────────────────────────────────────
def is_authenticated():
    """비밀번호가 없으면 항상 허용. 있으면 세션 확인."""
    if not SITE_PASSWORD:
        return True
    return session.get("auth") is True


# ─── 에러 핸들러: 모든 오류를 JSON으로 반환 (HTML 반환 방지) ──────
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": f"잘못된 요청: {e}"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "요청한 경로를 찾을 수 없습니다."}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "허용되지 않는 메서드입니다."}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": f"서버 내부 오류: {e}"}), 500

@app.errorhandler(Exception)
def unhandled_exception(e):
    return jsonify({"error": str(e)}), 500


# ─── CORS 헤더 ────────────────────────────────────────────────
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ─── Routes ───────────────────────────────────────────────────

@app.route("/")
def serve_index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/status")
def api_status():
    """클라이언트가 인증 상태 확인 시 사용."""
    return jsonify({
        "ok": True,
        "knowledge_loaded": _knowledge_cache is not None,
        "authenticated": is_authenticated(),
        "password_required": bool(SITE_PASSWORD),
    })


@app.route("/api/login", methods=["POST", "OPTIONS"])
def api_login():
    if request.method == "OPTIONS":
        return "", 204

    # 비밀번호가 설정 안 된 경우 바로 통과
    if not SITE_PASSWORD:
        session["auth"] = True
        return jsonify({"success": True})

    pw = (request.json or {}).get("password", "")
    if pw == SITE_PASSWORD:
        session["auth"] = True
        session.permanent = True
        return jsonify({"success": True})

    return jsonify({"error": "비밀번호가 올바르지 않습니다."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/generate", methods=["POST", "OPTIONS"])
def generate():
    if request.method == "OPTIONS":
        return "", 204

    # ── 인증 확인 ──
    if not is_authenticated():
        return jsonify({"error": "인증이 필요합니다."}), 401

    # ── OpenAI API 키 확인 (매 요청마다 환경변수를 직접 읽어 Railway 호환성 보장) ──
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({
            "error": "서버에 OPENAI_API_KEY 환경변수가 설정되지 않았습니다.\n"
                     "Railway Variables 탭에서 OPENAI_API_KEY를 설정해주세요."
        }), 500

    data = request.json or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "키워드를 입력해주세요."}), 400

    # ── OpenAI 호출 ──
    try:
        from openai import OpenAI
    except ImportError:
        return jsonify({"error": "openai 패키지가 없습니다. pip install openai 를 실행해주세요."}), 500

    knowledge = load_knowledge()
    system_prompt = build_system_prompt(knowledge)
    user_msg = (
        f"#UserInput: {keyword}\n\n"
        "에이전트 1→2→3→4를 순차 실행하고, 반드시 JSON 형식으로만 출력하세요."
    )

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=8000,
            temperature=0.3,
            response_format={"type": "json_object"},  # GPT-4o JSON 모드 보장
        )

        # ── 응답 유효성 확인 ──
        if not response.choices:
            return jsonify({"error": "AI가 응답을 반환하지 않았습니다. 다시 시도해주세요."}), 500

        raw = (response.choices[0].message.content or "").strip()
        finish_reason = response.choices[0].finish_reason

        if not raw:
            return jsonify({
                "error": f"AI 응답이 비어 있습니다 (finish_reason: {finish_reason}).\n"
                         "키워드를 더 구체적으로 입력하거나 다시 시도해주세요."
            }), 500

        # ── JSON 파싱 (보정 포함) ──
        try:
            cjm_data = json.loads(raw)
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*}", "}", raw)
            fixed = re.sub(r",\s*]", "]", fixed)
            try:
                cjm_data = json.loads(fixed)
            except json.JSONDecodeError as e:
                return jsonify({
                    "error": f"AI 응답 파싱 실패: {e}\n"
                             f"응답 미리보기: {raw[:300]}"
                }), 500

        return jsonify({"success": True, "data": cjm_data, "keyword": keyword})

    except Exception as e:
        err = str(e)
        if "api_key" in err.lower() or "authentication" in err.lower() or "incorrect" in err.lower():
            return jsonify({"error": "❌ OpenAI API 키가 올바르지 않습니다. Railway Variables에서 OPENAI_API_KEY를 확인해주세요."}), 401
        if "rate_limit" in err.lower():
            return jsonify({"error": "⏳ API 요청 한도 초과. 잠시 후 다시 시도해주세요."}), 429
        if "quota" in err.lower():
            return jsonify({"error": "💳 OpenAI 크레딧이 부족합니다. OpenAI 계정을 확인해주세요."}), 402
        return jsonify({"error": err}), 500


# ─── Main ─────────────────────────────────────────────────────
if __name__ == "__main__":
    is_public = os.environ.get("PUBLIC", "0") == "1"
    host = "0.0.0.0" if is_public else "localhost"
    port = int(os.environ.get("PORT", 5001))

    print("\n" + "=" * 52)
    print("🚀  SKT CJM Builder 시작!")
    print("=" * 52)
    if is_public:
        print(f"🌐  공개 모드: http://0.0.0.0:{port}")
        print(f"🔐  비밀번호: {'설정됨' if SITE_PASSWORD else '⚠ 미설정 (누구나 접근 가능)'}")
    else:
        print(f"📍  로컬 모드: http://localhost:{port}")
    print(f"🤖  모델: {OPENAI_MODEL}")
    print(f"🔑  API 키: {'✅ 설정됨' if OPENAI_API_KEY else '❌ 미설정 (.env 파일 확인)'}")
    print("⛔  종료: Ctrl + C")
    print("=" * 52)

    load_knowledge()
    app.run(debug=False, port=port, host=host)
