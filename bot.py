# 이 파일을 리포지토리의  .github/workflows/news.yml  경로에 두세요.
#
# cron 은 UTC 기준입니다 (KST = UTC + 9):
#   21:00 UTC → 06:00 KST (다음날)
#   04:00 UTC → 13:00 KST
#   09:00 UTC → 18:00 KST
# bot.py 가 KST 기준으로 SEND_HOURS 를 한 번 더 확인하므로,
# GitHub Actions 가 몇 분 늦게 실행돼도 같은 '시' 안이면 정상 발송됩니다.

name: News Run

on:
  schedule:
    - cron: "0 21,4,9 * * *"
  workflow_dispatch:          # Actions 탭에서 수동 실행 버튼
    inputs:
      force:
        description: "시간대 무시하고 즉시 발송"
        type: boolean
        default: true

jobs:
  brief:
    runs-on: ubuntu-latest
    timeout-minutes: 30

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      # seen.json 을 실행 간에 보존해 같은 기사가 반복 발송되는 것을 막습니다.
      # (Actions 는 매 실행이 새 머신이라 캐시가 없으면 기억이 사라집니다)
      - name: Restore seen.json
        uses: actions/cache/restore@v4
        with:
          path: seen.json
          key: seen-${{ github.run_id }}
          restore-keys: seen-

      - name: Send brief
        env:
          TELEGRAM_BOT_TOKEN:  ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID:    ${{ secrets.TELEGRAM_CHAT_ID }}
          NAVER_CLIENT_ID:     ${{ secrets.NAVER_CLIENT_ID }}
          NAVER_CLIENT_SECRET: ${{ secrets.NAVER_CLIENT_SECRET }}
          # 요약 LLM — 아래 vars.LLM_PROVIDER 를 gemini / groq / anthropic / none 으로 설정.
          # (Settings > Secrets and variables > Actions > Variables 탭)
          # 미설정 시 gemini 가 기본값이며, 키가 없으면 스니펫 요약으로 동작합니다.
          LLM_PROVIDER:        ${{ vars.LLM_PROVIDER || 'gemini' }}
          GEMINI_API_KEY:      ${{ secrets.GEMINI_API_KEY }}
          GROQ_API_KEY:        ${{ secrets.GROQ_API_KEY }}
          ANTHROPIC_API_KEY:   ${{ secrets.ANTHROPIC_API_KEY }}
        run: python bot.py ${{ inputs.force && '--now' || '' }}

      - name: Save seen.json
        if: always()
        uses: actions/cache/save@v4
        with:
          path: seen.json
          key: seen-${{ github.run_id }}
