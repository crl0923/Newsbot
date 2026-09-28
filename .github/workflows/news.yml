name: news-brief

on:
  schedule:
    # ★ 2026-09-28: GitHub cron 은 실제로 2~3시간씩 늦게 돈다
    #   (05:37 예약 → 07:30~08:30 KST 에 시작해서 브리프가 8시쯤 왔다).
    #   그래서 새벽 2시대로 크게 당겨 두고, 일찍 깨면 bot.py 가
    #   05:30 까지 잔 뒤 수집 → 06:00 에 발송한다.
    # 두 번째 줄은 예비. 하루 1회 가드가 있어서 두 번 오지 않는다
    # (먼저 돈 쪽이 보내면 나머지는 "오늘 이미 발송됨" 으로 끝난다).
    - cron: "13 17 * * *"   # 02:13 KST
    - cron: "43 18 * * *"   # 03:43 KST (예비)
  workflow_dispatch:
    inputs:
      force:
        description: "오늘 이미 보냈어도 다시 발송"
        type: boolean
        default: false

# 같은 워크플로가 겹쳐 돌지 않게 한다 (cron 과 수동 실행이 겹치는 경우 등)
concurrency:
  group: news-brief
  cancel-in-progress: false

jobs:
  brief:
    runs-on: ubuntu-latest
    timeout-minutes: 330   # 새벽에 깨어 05:30 까지 자는 시간 포함 (최대 360)
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      # Claude 구독으로 LLM 을 쓰기 위한 CLI
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: npm install -g @anthropic-ai/claude-code

      # seen-auto.json / seen-other.json 복원 (실행 간 중복 방지 + 하루 1회 가드)
      - uses: actions/cache/restore@v4
        with:
          path: |
            seen-auto.json
            seen-other.json
          key: seen-${{ github.run_id }}
          restore-keys: seen-

      - name: Run bot
        env:
          TELEGRAM_BOT_TOKEN:      ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID:        ${{ secrets.TELEGRAM_CHAT_ID }}
          NAVER_CLIENT_ID:         ${{ secrets.NAVER_CLIENT_ID }}
          NAVER_CLIENT_SECRET:     ${{ secrets.NAVER_CLIENT_SECRET }}
          LLM_PROVIDER:            claude_code
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          CLAUDE_CODE_MODEL:       haiku
          PREP_AT:                 "05:30"
          SEND_AT:                 "06:00"
          HOLD_MAX_MIN:            "240"
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            EXTRA="--no-hold"
            [ "${{ inputs.force }}" = "true" ] && EXTRA="$EXTRA --force-send"
          fi
          python bot.py --now --group all $EXTRA

      # 봇이 실패해도 seen 은 저장한다 (발송된 기사가 다음 날 또 오지 않게)
      - uses: actions/cache/save@v4
        if: always()
        with:
          path: |
            seen-auto.json
            seen-other.json
          key: seen-${{ github.run_id }}
