#!/bin/bash
# ────────────────────────────────────────────────
#  CJM Builder · 공개 URL 실행 (동료 공유용)
#  ngrok 을 이용해 외부에서 접근 가능한 URL 생성
#
#  사전 준비:
#    1. https://ngrok.com 에서 무료 계정 생성
#    2. 터미널에서: brew install ngrok  (또는 ngrok.com에서 다운로드)
#    3. 터미널에서: ngrok config add-authtoken <ngrok 토큰>
# ────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "======================================"
echo "  🌍 SKT CJM Builder (공개 모드)"
echo "======================================"

# .env 확인
if [ ! -f ".env" ]; then
  echo ""
  echo "❌ .env 파일이 없습니다!"
  echo "   cp .env.example .env  후 API 키와 비밀번호를 입력해주세요."
  exit 1
fi

# SITE_PASSWORD 경고
if ! grep -q "^SITE_PASSWORD=.\+" .env 2>/dev/null; then
  echo ""
  echo "⚠  경고: SITE_PASSWORD가 설정되지 않았습니다!"
  echo "   공개 URL로 누구나 접근할 수 있습니다."
  echo "   .env 파일에 SITE_PASSWORD=비밀번호 를 설정하세요."
  echo ""
  read -p "그래도 계속하려면 엔터..."
fi

# ngrok 확인
if ! command -v ngrok &>/dev/null; then
  echo ""
  echo "❌ ngrok가 설치되지 않았습니다."
  echo ""
  echo "   설치 방법 (Mac):"
  echo "   1) 터미널에서: brew install ngrok"
  echo "   2) https://ngrok.com 에서 무료 가입 후 authtoken 등록"
  echo "      ngrok config add-authtoken <토큰>"
  echo ""
  exit 1
fi

# 패키지 설치
echo "📦 패키지 확인 중..."
pip3 install flask openai python-docx openpyxl --quiet 2>/dev/null \
  || pip install flask openai python-docx openpyxl --quiet 2>/dev/null

# Flask 서버 시작 (공개 모드)
echo "🌐 Flask 서버 시작 중..."
PUBLIC=1 python3 app.py &
FLASK_PID=$!
sleep 3

# ngrok 시작
echo "🔗 ngrok 터널 시작 중..."
ngrok http 5001 --log=stdout 2>&1 &
NGROK_PID=$!
sleep 3

# ngrok URL 가져오기
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null)

echo ""
echo "======================================"
echo "  ✅ 공개 URL이 준비되었습니다!"
echo ""
if [ -n "$NGROK_URL" ]; then
  echo "  🌍 공개 주소: $NGROK_URL"
else
  echo "  🌍 공개 주소: http://localhost:4040 에서 확인"
fi
echo ""
echo "  📋 이 URL을 동료들과 공유하세요"
echo "  🔐 비밀번호가 설정된 경우 함께 전달하세요"
echo ""
echo "  ⚠  주의: ngrok 무료플랜은 URL이 매번 바뀝니다"
echo "  ⛔ 종료: Ctrl + C"
echo "======================================"
echo ""

# 정리 함수
cleanup() {
  echo "서버 종료 중..."
  kill $FLASK_PID 2>/dev/null
  kill $NGROK_PID 2>/dev/null
  exit 0
}
trap cleanup SIGINT SIGTERM

wait
