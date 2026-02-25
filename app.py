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
from flask import Flask, request, jsonify, send_from_directory, session, Response, stream_with_context

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


def extract_docx_text(filepath, max_chars=15000):
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


def extract_xlsx_text(filepath, max_chars=8000):
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
    return f"""당신은 통신 서비스에 특화된 'CJM(Customer Journey Map) 자동화 멀티 에이전트 빌더'입니다. 당신의 내부에는 3개의 전문 에이전트가 존재하며, 사용자가 #UserInput(자유 유형. 예: 사용자 유형, 액션, Context 등)을 입력하면 아래의 에이전트 1, 2, 3의 역할을 순차적으로 수행하여 최종 아웃풋을 만들어내야 합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 1: 쿼리 확장 에이전트]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* 역할: 당신은 #UserInput을 첨부된 데이터(Knowledge)를 기준으로 쿼리를 확장하는 에이전트입니다.
* 쿼리 확장 지침:
   1. #UserInput을 통신사에서 제공하는 채널(서비스)에서 제공할 수 있는 서비스로 한정해서 확장합니다.
   2. 통신사에서 제공하는 서비스 정의:
      * Tworld: 통신 관리 서비스. 요금제 변경, 번호 이동, 요금 납부, 데이터 충전, 데이터 선물 등 가입한 통신 서비스에 대한 전반적인 유틸리티 관련 업무 수행
      * T멤버십: 통신사에 가입한 고객에게 주는 혜택 서비스. 할인, 적립, 쿠폰 등 다양한 온오프라인 혜택 제공
      * T우주: 여러가지 상품 혹은 단일 상품을 구독화하여 할인을 제공하는 서비스 (예: Youtube premium + Google One. 월 9,900원으로 스타벅스 쿠폰 3장 지급 등)
      * T다이렉트샵: 단말 구매부터 개통까지 온라인으로 진행할 수 있는 커머스 서비스
      * 고객센터: 콜센터. 통신 가입, 해지, CS등 전반적인 업무를 진행
      * 대리점: 오프라인 대리점으로, 통신 가입, 해지, CS등 전반적인 업무를 진행해주는 공간
   3. 쿼리는 복수 개로 확장할 수 있습니다. (최대 5개). #UserInput이 모호하거나 축약되어 있을 경우, 쿼리를 복수 개로 확장합니다.
* 쿼리 확장 Output 포맷 및 변수 정의:
   * {{#TargetSegment}}의 {{#Channel}}에서 {{#Action}} 여정을 만드세요 포맷으로 만듭니다.
   * #TargetSegment: 나이대와 사용자 특성, 컨텍스트를 모두 반영하여 제작 (예시: 40대 여성, 30대 1인 가구, 일반인(보편적인 Target), 50대 액티브 시니어)
   * #Channel: 사용자가 액션을 수행하기 위한 채널 (Tworld, T멤버십, T우주, T다이렉트샵, 고객센터, 대리점)
   * #Action: 통신 관련 유저 액션, 과업을 지칭 (예시: 번호 이동, 기기 변경, 미납 요금 확인, 청구서 확인, 할인 쿠폰 조회 등)
* 예시:
   * 예시 1) Input: 40대 여성 번호 이동 → Output: {{40대 여성}}의 {{대리점}}에서 {{번호 이동}} 여정을 만드세요
   * 예시 2) Input: 여행 → Output: {{여행을 가는 일반인}}의 {{Tworld}}에서 {{로밍 가입}} 여정을 만드세요
   * 예시 3) Input: 가입 → Output:
      1. {{일반인}}의 {{T다이렉트샵}}에서 {{번호 신규 가입}} 여정을 만드세요
      2. {{일반인}}의 {{대리점}}에서 {{번호 신규 가입}} 여정을 만드세요
      3. {{일반인}}의 {{T우주}}에서 {{구독상품 가입}} 여정을 만드세요

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 2: Journey 제작 에이전트]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* 역할: 당신은 에이전트 1이 확장한 쿼리를 바탕으로, 첨부된 데이터(Knowledge)를 기반으로 Journey를 만드는 에이전트입니다.
* Journey 제작 지침:
   1. Journey는 #UserInput(확장된 쿼리)을 수행하기 위한 전체 여정을 최소 6단계, 최대 8단계로 만듭니다.
   2. 예를 들어, '{{40대 여성}}의 {{대리점}}에서 {{번호 이동}} 여정을 만드세요'라는 쿼리가 들어오면, 대리점에서 번호 이동을 하기 위한 실제 절차를 시간 순서대로 Journey로 만듭니다.
   3. 각 단계는 phase(구간)로 그룹화합니다 (예: 인지→탐색→결정→실행→완료).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 3: 데이터 기반 User Action, Painpoint, Needs, Insight 작성 에이전트 ⭐ 핵심]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* 역할: 당신은 에이전트 2가 만든 Journey 단계 별로(Y축), 실제 유저가 각 Journey에서 실행하는 요소를 '첨부된 데이터(Knowledge) 및 업로드된 자료'를 기준으로 각각 작성하는 에이전트입니다. 당신은 꼭 첨부된 데이터를 기반으로 자료를 작성합니다.
   * 각 항목별 내용은 실제 사용자의 인터뷰 데이터를 기반으로 한 것처럼 작성합니다.
   * 예시)
      * User Action: 대리점에 가기 전에 어떤 내용을 물어볼지 미리 생각함. 대리점에서 개인정보를 검색하고, 새로운 요금제를 영업함. Tworld의 요금안내서에서 상세 요금 내역을 확인함.
      * Feeling: "이 '기타 사용료' 3천 원은 뭐지?", "최근 3개월치 평균을 보고 싶은데..", "데이터 선물하기는 얼마나 할 수 있는거지?"
* 제작 지침:
   * User Action (Behavior): 해당 단계에서 유저가 구체적으로 취하는 행동. 단계를 건너뛰지 않고 촘촘하게 작성
   * Feeling: 액션을 수행하면서 느끼는 유저의 감정 상태
   * Painpoint: 액션을 수행하면서 유저가 겪는 어려움, 불편함
   * Needs: 유저가 해당 단계에서 바라는 점. 숨은 Needs나 가장 핵심이 되는 Needs
   * Insight: 이를 해결하기 위한 서비스적 기회나 솔루션
* 출처 표기:
   * 작성한 데이터의 출처를 기입합니다. 파일명과 참조한 영역(섹션명, 발언자, 핵심 내용 30자 이내 요약)을 표시합니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[에이전트 4: 검색 기반 User Action, Painpoint, Needs, Insight 작성 에이전트]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
* 역할: 당신은 에이전트 2가 만든 Journey 단계 별로(Y축)에, 실제 유저가 각 Journey에서 실행하는 요소를 신빙성있는 자료 검색을 기반으로 작성합니다. 에이전트3가 발견하지 못한 핵심적이고 중요한 요소들을 채워 넣는 역할을 합니다. 특히 잠재적인 Needs를 파악하는데 집중합니다.
* 제작 지침:
   * User Action (Behavior): 해당 단계에서 유저가 구체적으로 취하는 행동. 단계를 건너뛰지 않고 촘촘하게 작성
   * Feeling: 액션을 수행하면서 느끼는 유저의 감정 상태
   * Painpoint: 액션을 수행하면서 유저가 겪는 어려움, 불편함
   * Needs: 유저가 해당 단계에서 바라는 점. 숨은 Needs나 가장 핵심이 되는 Needs
   * Insight: 이를 해결하기 위한 서비스적 기회나 솔루션

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[첨부된 데이터 - Knowledge]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{knowledge}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[출력 규칙 - 반드시 JSON으로 출력]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
에이전트 1→2→3→4 순서로 사고한 후, 반드시 아래 JSON 구조로만 최종 출력하세요.
에이전트3(knowledge) 데이터가 핵심이며 하이라이트 표시됩니다. 에이전트4(search) 데이터로 보완합니다.
출처(source_detail)는 파일명과 참조한 섹션/발언자/핵심내용을 함께 기재합니다.

⚠ 응답 시간 제약 (중요): 서버 응답 제한(60초) 준수를 위해 아래 제약을 반드시 지키세요.
- 에이전트1 쿼리 확장: 최대 3개까지만 생성 (입력이 명확하면 1~2개)
- 각 셀(knowledge/search)마다 항목 1개씩만 작성 (핵심만 간결하게)
- 전체 JSON 길이를 최소화하여 빠르게 완성할 것

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
            "knowledge": [{{"text": "구체적 행동 서술", "source": "파일명.docx", "source_detail": "파일명.docx - [섹션명/발언자/핵심내용 요약]"}}],
            "search": [{{"text": "구체적 행동 서술"}}]
          }},
          "feeling": {{
            "knowledge": [{{"text": "\\"인용구 형식의 감정\\"", "source": "파일명.docx", "source_detail": "파일명.docx - [섹션명/발언자/핵심내용 요약]"}}],
            "search": [{{"text": "\\"인용구 형식의 감정\\""}}]
          }},
          "painpoint": {{
            "knowledge": [{{"text": "불편함 서술", "source": "파일명.docx", "source_detail": "파일명.docx - [섹션명/발언자/핵심내용 요약]"}}],
            "search": [{{"text": "불편함 서술"}}]
          }},
          "needs": {{
            "knowledge": [{{"text": "요구사항 서술", "source": "파일명.docx", "source_detail": "파일명.docx - [섹션명/발언자/핵심내용 요약]"}}],
            "search": [{{"text": "요구사항 서술"}}]
          }},
          "insight": {{
            "knowledge": [{{"text": "솔루션 제안", "source": "파일명.docx", "source_detail": "파일명.docx - [섹션명/발언자/핵심내용 요약]"}}],
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


def _sse(event_dict: dict) -> str:
    """SSE 이벤트 포맷으로 변환."""
    return "data: " + json.dumps(event_dict, ensure_ascii=False) + "\n\n"


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

    # ── SSE 스트림 생성기 ──────────────────────────────────────────
    # SSE 방식으로 토큰이 생성되는 동안 계속 데이터를 전송하면
    # Railway의 60초 HTTP 타임아웃이 적용되지 않습니다.
    def generate_sse():
        try:
            client = OpenAI(api_key=api_key)

            # 시작 알림
            yield _sse({"type": "progress", "msg": "🤖 에이전트 1: 쿼리 확장 중..."})

            collected = []
            token_count = 0

            # ── OpenAI 스트리밍 호출 ──
            with client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_completion_tokens=16000,
                temperature=0.3,
                response_format={"type": "json_object"},
                stream=True,
            ) as stream:
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        collected.append(token)
                        token_count += 1

                        # 100토큰마다 진행 상황 알림 (연결 유지 + UI 업데이트)
                        if token_count % 100 == 0:
                            if token_count < 300:
                                msg = "🗺 에이전트 2: Journey 단계 설계 중..."
                            elif token_count < 1500:
                                msg = "📋 에이전트 3: 데이터 기반 CJM 작성 중..."
                            else:
                                msg = "🔍 에이전트 4: UX 관점 보완 중..."
                            yield _sse({"type": "progress", "msg": msg})

            # ── 완성된 JSON 파싱 ──
            raw = "".join(collected).strip()

            if not raw:
                yield _sse({"type": "error", "error": "AI 응답이 비어 있습니다. 다시 시도해주세요."})
                return

            try:
                cjm_data = json.loads(raw)
            except json.JSONDecodeError:
                fixed = re.sub(r",\s*}", "}", raw)
                fixed = re.sub(r",\s*]", "]", fixed)
                try:
                    cjm_data = json.loads(fixed)
                except json.JSONDecodeError as e:
                    yield _sse({"type": "error",
                                "error": f"AI 응답 파싱 실패: {e}\n미리보기: {raw[:300]}"})
                    return

            yield _sse({"type": "result", "data": cjm_data, "keyword": keyword})
            yield "data: [DONE]\n\n"

        except Exception as e:
            err = str(e)
            if "api_key" in err.lower() or "authentication" in err.lower() or "incorrect" in err.lower():
                yield _sse({"type": "error", "error": "❌ OpenAI API 키가 올바르지 않습니다. Railway Variables에서 OPENAI_API_KEY를 확인해주세요."})
            elif "rate_limit" in err.lower():
                yield _sse({"type": "error", "error": "⏳ API 요청 한도 초과. 잠시 후 다시 시도해주세요."})
            elif "quota" in err.lower():
                yield _sse({"type": "error", "error": "💳 OpenAI 크레딧이 부족합니다. OpenAI 계정을 확인해주세요."})
            else:
                yield _sse({"type": "error", "error": err})

    return Response(
        stream_with_context(generate_sse()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # nginx/Railway 버퍼링 비활성화
            "Connection": "keep-alive",
        },
    )


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
