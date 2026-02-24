#!/bin/bash
# ────────────────────────────────────────────────
#  CJM Builder · 로컬 실행 (개인 사용)
#  공개 URL이 필요하면 start_public.sh 를 사용하세요
# ────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "======================================"
echo "  🚀 SKT CJM Builder (로컬 모드)"
echo "======================================"

# .env 확인
if [ ! -f ".env" ]; then
  echo ""
  echo "⚠  .env 파일이 없습니다!"
  echo "   1) .env.example 파일을 복사해서 .env 로 저장"
  echo "   2) OPENAI_API_KEY 값을 채워주세요"
  echo ""
  echo "   cp .env.example .env"
  echo "   그 다음 .env 파일을 텍스트 편집기로 열어 API 키 입력"
  echo ""
  read -p "계속하려면 엔터를 누르세요..."
fi

# 패키지 설치
echo ""
echo "📦 패키지 확인 중..."
pip3 install flask openai python-docx openpyxl --quiet 2>/dev/null \
  || pip install flask openai python-docx openpyxl --quiet 2>/dev/null

# 서버 시작
echo "🌐 서버 시작 중..."
python3 app.py &
SERVER_PID=$!
sleep 2

# 브라우저 열기
open "http://localhost:5001"

echo ""
echo "======================================"
echo "  ✅ 실행 중: http://localhost:5001"
echo "  ⛔ 종료: Ctrl + C"
echo "======================================"
echo ""

wait $SERVER_PID
