#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Equity Research News Bot — Naver News + LLM (공급자 교체 가능)
- 커버리지는 두 그룹으로 분리되어 각각 독립 실행된다:
    auto  : 한국 자동차·자동차부품 + European automakers
    other : EV/Battery, Construction, Shipbuilding
- 본문: 기사 첫 두 문장을 '그대로' 발췌한다(모델이 쓰는 요약이 아니다).
- 언어: 기본 영어. 발췌한 두 문장을 LLM 이 '번역'만 한다 (BRIEF_LANG=ko|both).
- LLM: Gemini / Groq / Anthropic 중 택1 (LLM_PROVIDER 환경변수).
  키가 없거나 한도 초과여도 봇은 멈추지 않고 한국어 원문으로 발송된다.
- GitHub Actions 에서 1회성으로 실행되는 구조 (상주 프로세스 아님)

- 매일 06:00 KST 1회 발송. 스케줄은 .github/workflows/news.yml 의 cron 이 쥔다.

실행:
    python bot.py --group auto     # 자동차 브리프만
    python bot.py --group other    # 비자동차 브리프만
    python bot.py --group all      # 둘 다 (기본값)
    python bot.py --now            # 시간대 무시하고 즉시 발송 (Actions / 테스트용)
    python bot.py --dry-run        # 텔레그램 발송 없이 콘솔에만 출력

★ 그룹마다 seen 파일이 분리된다(seen-auto.json / seen-other.json). 두 잡이
  같은 파일을 공유하면 Actions 캐시가 서로를 덮어써 중복 발송이 되살아난다.

★ Actions 워크플로는 --now 로 돌린다. GitHub 의 cron 은 수 분~십수 분 밀리는
  일이 흔해서, 21:00 UTC 예약이 07:0x KST 에 실행되면 시간대 게이트에 걸려
  그날 브리프가 통째로 사라진다. 스케줄은 cron 한 곳에서만 관리한다.
"""

import os
import re
import sys
import json
import time
import html
import logging
import requests

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

try:
    # ★ stderr 도 함께. logging 은 stderr 로 나가므로 stdout 만 바꾸면
    #   윈도우 콘솔에서 한글 로그가 전부 깨져 디버깅이 불가능해진다.
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN           = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID             = os.getenv("TELEGRAM_CHAT_ID")
NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

# ★ Windows 에는 시스템 tz 데이터베이스가 없어서 ZoneInfo("Asia/Seoul") 이
#   ZoneInfoNotFoundError 로 죽는다 (Actions 의 ubuntu 에서는 정상).
#   requirements 의 tzdata 가 이를 해결하지만, 없더라도 죽지 않게 폴백을 둔다.
#   KST 는 1988년 이후 DST 가 없는 고정 UTC+9 이라 오프셋 대체가 정확하다.
try:
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

TELEGRAM_MAX_CHARS = 3800
MAX_PER_COMPANY    = 3        # 회사(peer)당 최대 기사 수
SKIP_WEEKEND       = False    # True 로 두면 토/일 미발송
SEND_HOURS         = {6}      # KST 기준 발송 허용 '정시대' — 매일 06:00 1회

# 중복 판별 — 0.4~0.6 사이 권장.
# 너무 낮추면(예: 0.05) 무관한 기사끼리도 상투어가 겹쳐 전부 중복 처리된다.
DEDUP_THRESHOLD  = 0.45
DEDUP_MIN_SHARED = 5

# 실행 간 중복 방지 (GitHub Actions 캐시로 보존). 그룹별로 파일이 갈린다 —
# seen_path() 참고. 환경변수로 덮어쓸 수 있다.
SEEN_FILE      = os.getenv("SEEN_FILE", "")
SEEN_KEEP_DAYS = 14

# ── LLM 설정 ────────────────────────────────────────────────────────────────
# LLM_PROVIDER 환경변수로 교체:  gemini | groq | anthropic | none
#   gemini    : 무료. GEMINI_API_KEY      (aistudio.google.com)
#   groq      : 무료 티어가 가장 넉넉함. GROQ_API_KEY   (console.groq.com)
#   anthropic : 유료지만 배치 처리 시 월 $2~3 수준. ANTHROPIC_API_KEY
#   none      : LLM 없이 스니펫 요약만 사용 (완전 무료, 품질 낮음)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

# 한 번의 호출에 넣을 기사 수. 호출 수를 줄이는 핵심이지만, 크게 잡을수록
# ★ 응답 JSON 이 출력 토큰 한도에 걸려 뒤쪽 기사가 통째로 잘린다. 한국어는
#   토큰 효율이 나빠(음절당 1.5~3 토큰) 10건이면 2,000 토큰을 쉽게 넘긴다.
LLM_BATCH      = 6
LLM_MAX_TOKENS = 8000   # 잘린 JSON = 그 배치 전체가 스니펫 요약으로 떨어진다
SUMMARY_MAX_CHARS = 80   # 폴백(스니펫) 경로에서만 쓰인다

# ── 본문 리드 발췌 + 영어 번역 ───────────────────────────────────────────────
# ★ 2026-08-18: 모델에게 '요약'을 시키지 않는다.
#   요약을 시키면 문체가 기사마다 달라진다 — 어떤 건 '~했다', 어떤 건 음슴체.
#   길이 제한에 맞추려고 모델이 문장을 스스로 잘라내기도 한다. 이제는
#   기사 본문의 첫 두 문장을 '그대로' 가져온 뒤, 번역만 시킨다. 문체는
#   원문(=기사 리드)이 정하고 모델은 옮기기만 하므로 편차가 없다.
# ★ 브리프 언어는 영어가 기본. BRIEF_LANG=ko 면 번역 없이 한국어 원문,
#   both 면 영어 아래에 한국어 원제를 같이 붙인다.
LEAD_SENTENCES  = 2      # 가져올 문장 수
LEAD_MAX_CHARS  = 240    # 한 문장이 이보다 길면 어절 경계에서 자른다
LEAD_FETCH      = os.getenv("LEAD_FETCH", "1") != "0"   # 0 이면 스니펫만 사용
LEAD_TIMEOUT    = 8
TRANSLATE_BATCH = 5      # 번역은 출력이 길어 판정(6)보다 작게 잡는다
BRIEF_LANG      = os.getenv("BRIEF_LANG", "en").strip().lower()   # en | ko | both

PROVIDERS = {
    "gemini":    {"key": os.getenv("GEMINI_API_KEY", ""),
                  "model": "gemini-2.5-flash-lite", "interval": 4.5},
    "groq":      {"key": os.getenv("GROQ_API_KEY", ""),
                  "model": "llama-3.3-70b-versatile", "interval": 2.5},
    "anthropic": {"key": os.getenv("ANTHROPIC_API_KEY", ""),
                  "model": "claude-haiku-4-5-20251001", "interval": 1.0},
    "none":      {"key": "", "model": "", "interval": 0},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Coverage universe ────────────────────────────────────────────────────────
# 각 섹터는 {peer, context, entries} 구조.
#   entries 튜플 = (표시명, 검색어, 제목 매칭 키워드)
#   peer=True  → 해외 기업. 네이버 검색은 한글 표기로 하고, 표시는 영문명으로.
#   context    → LLM 프롬프트에 넣는 애널리스트 관점. 섹터마다 다르다.
KOREA_AUTO = [
    ("Hyundai Motor", "현대자동차", ["현대자동차", "현대차"]),
    ("Kia",           "기아",       ["기아"]),
    ("Hyundai Mobis", "현대모비스", ["현대모비스"]),
    ("HL Mando",      "HL만도",     ["HL만도", "만도"]),
    ("Hankook Tire",  "한국타이어", ["한국타이어", "한국앤컴퍼니"]),
    ("Hanon Systems", "한온시스템", ["한온시스템"]),
]

# ★ European peer 는 VW / BMW / Stellantis 세 곳만. (2026-08-18)
#   일본·미국 peer(도요타·혼다·닛산·Tesla·GM)는 물론이고, 예전에 넣었던
#   Mercedes / Renault / Porsche / Ferrari / Volvo 도 커버리지 밖이라 뺐다.
#   다시 넣으려면 이 리스트에 줄만 추가하면 된다.
#   검색어는 한글 표기가 히트율이 높다(BMW 처럼 영문이 통용되는 곳만 예외).
EUROPEAN_AUTO = [
    ("Volkswagen",    "폭스바겐",   ["폭스바겐", "폴크스바겐", "VW"]),
    ("BMW",           "BMW",        ["BMW"]),
    ("Stellantis",    "스텔란티스", ["스텔란티스"]),
]

AUTO_COVERAGE: dict[str, dict] = {
    "Korea Auto / Auto Parts": {
        "peer": False,
        "context": "한국 자동차·자동차부품 섹터",
        "entries": KOREA_AUTO,
    },
    "European Automakers": {
        "peer": True,
        "context": "유럽 완성차(OEM) 업종 — 한국 자동차 섹터와의 경쟁구도·전방수요 관점",
        "entries": EUROPEAN_AUTO,
    },
}

OTHER_COVERAGE: dict[str, dict] = {
    "EV / Battery": {
        "peer": False,
        "context": "한국 주식 2차전지 섹터",
        "entries": [
            ("LG Energy Solution", "LG에너지솔루션", ["LG에너지솔루션", "LG엔솔", "LGES"]),
            ("Samsung SDI",        "삼성SDI",        ["삼성SDI"]),
            ("SK Innovation",      "SK이노베이션",   ["SK이노베이션", "SK온"]),
            ("POSCO Future M",     "포스코퓨처엠",   ["포스코퓨처엠"]),
            ("L&F",                "엘앤에프",       ["엘앤에프", "L&F"]),
        ],
    },
    "Construction": {
        "peer": False,
        "context": "한국 주식 건설 섹터",
        "entries": [
            ("Hyundai E&C", "현대건설", ["현대건설"]),
            ("GS E&C",      "GS건설",   ["GS건설"]),
            ("Samsung E&A", "삼성E&A",  ["삼성E&A", "삼성엔지니어링"]),
            ("Samsung C&T", "삼성물산", ["삼성물산"]),
        ],
    },
    "Shipbuilding": {
        "peer": False,
        "context": "한국 주식 조선 섹터",
        "entries": [
            ("Hanwha Ocean",    "한화오션",       ["한화오션"]),
            ("Samsung Heavy",   "삼성중공업",     ["삼성중공업"]),
            ("HD Hyundai HI",   "HD현대중공업",   ["HD현대중공업", "현대중공업"]),
            ("Hanwha Engine",   "한화엔진",       ["한화엔진"]),
            ("HD Hyundai KSOE", "HD한국조선해양", ["HD한국조선해양", "한국조선해양", "KSOE"]),
        ],
    },
}

# --group 으로 고르는 실행 단위. 각각 별도의 seen 파일 / 별도의 Actions 잡.
GROUPS: dict[str, dict] = {
    "auto":  {"title": "Auto News Brief",     "coverage": AUTO_COVERAGE},
    "other": {"title": "Non-Auto News Brief", "coverage": OTHER_COVERAGE},
}

# ── 규칙 기반 노이즈 필터 ─────────────────────────────────────────────────────
# ★ LLM 호출 전에 무료로 걸러내는 단계. 무료 티어 사용량을 좌우하는 핵심.
#   제목에만 적용한다(본문에 적용하면 정상 기사도 과하게 걸린다).
# HARD: 무조건 버린다. 홍보성 기사는 '계약'·'개발' 같은 단어가 섞여 있어도
#       펀더멘털 뉴스가 아니므로 구제하지 않는다.
#       (예: "프로야구단 스폰서 계약 체결" — '계약'이 있다고 살리면 안 된다)
HARD_NOISE_RE = re.compile(
    r"봉사|기부|후원|사회공헌|장학|헌혈|캠페인|나눔|연탄|김장|"
    r"스폰서|후원사|프로야구|축구단|구단|e스포츠|올림픽\s*후원|"
    r"채용\s*설명회|공채|인턴\s*모집|사내\s*행사|창립기념|"
    r"우수기업\s*선정|대상\s*수상|공모전|"
    # ★ 2026-08-18 추가: 재단·CSR 성격의 육성/교육 프로그램 기사가 계속 통과했다.
    #   (예: "현대차 정몽구재단, 캠퍼스프러너 부트캠프") — '지원'·'개발'이 들어
    #   있어 RESCUE 로도 살아남으므로 HARD 에 둔다.
    r"재단|부트캠프|해커톤|멘토링|경진대회|아카데미|"
    r"창업\s*(지원|경진|교육)|산학협력|위촉|명예\s*박사|전시회\s*참가"
)

# SOFT: 주가·수급 기사. 펀더멘털 키워드가 같이 있으면 살린다.
#       (예: "목표주가 상향… 수주잔고 사상 최대" — 뒤쪽이 본론)
SOFT_NOISE_RE = re.compile(
    r"52주\s*(신고가|신저가)|신고가|신저가|상한가|하한가|"
    r"목표주가|투자의견|매수\s*의견|\'?매수\'?\s*유지|"
    r"순매수|순매도|[0-9.]+%\s*(상승|하락|급등|급락|강세|약세)|"
    r"(상승|하락|급등|급락|강세|약세)\s*(마감|출발|전환)|"
    r"코스피|코스닥|증시|시황|주가\s*(상승|하락|급등|급락)|"
    r"시가총액|공매도|배당락"
)

# SOFT 노이즈를 구제하는 펀더멘털 키워드
RESCUE_RE = re.compile(
    r"수주|계약|실적|영업이익|매출|가이던스|증설|capex|설비투자|"
    r"인수|합병|지분|공장|가동|생산|수출|리콜|소송|파업|"
    r"신차|신제품|기술|특허|개발|공급|납품|규제|관세|정책",
    re.IGNORECASE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)


def news_window_hours() -> int:
    return 72 if now_kst().weekday() == 0 else 24


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text.strip()


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def title_has_company(title: str, keywords: list[str]) -> bool:
    return any(kw.lower() in title.lower() for kw in keywords)


def is_noise(title: str) -> bool:
    """규칙 기반 1차 필터 (LLM 호출 전, 무료).
    HARD 노이즈는 무조건 버리고, SOFT 노이즈는 펀더멘털 키워드가 없을 때만 버린다."""
    if HARD_NOISE_RE.search(title):
        return True
    if SOFT_NOISE_RE.search(title) and not RESCUE_RE.search(title):
        return True
    return False


def snippet_words(text: str) -> set[str]:
    text = re.sub(r"[^0-9A-Za-z가-힣]+", " ", text)
    return {w for w in text.split() if len(w) >= 2}


def is_content_duplicate(new_art: dict, accepted: list[dict]) -> bool:
    new_words = snippet_words(new_art["snippet"])
    if len(new_words) < DEDUP_MIN_SHARED:
        return False
    for art in accepted:
        existing = snippet_words(art["snippet"])
        if len(existing) < DEDUP_MIN_SHARED:
            continue
        shared = new_words & existing
        if (len(shared) / len(new_words | existing) >= DEDUP_THRESHOLD
                and len(shared) >= DEDUP_MIN_SHARED):
            return True
    return False


# ── seen.json (실행 간 중복 방지) ─────────────────────────────────────────────
def seen_path(group: str) -> str:
    """★ 그룹별로 파일을 나눈다. auto / other 잡이 같은 seen.json 을 쓰면
    Actions 캐시가 서로를 덮어써 한쪽의 기억이 통째로 날아간다."""
    return SEEN_FILE or f"seen-{group}.json"


def load_seen(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(path: str, seen: dict) -> None:
    cutoff = (now_kst() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=0)
        logger.info("%s 저장 — %d건 보관", path, len(seen))
    except Exception as exc:
        logger.warning("%s 저장 실패: %s", path, exc)


# ── Naver ────────────────────────────────────────────────────────────────────
def fetch_naver_news(query: str, hours: int) -> list[dict]:
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": 100 if hours >= 72 else 50, "sort": "date"}

    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            time.sleep(0.3)          # 네이버 연속 호출 간격
            data = r.json()
            break
        except Exception as exc:
            if attempt == 2:
                logger.error("Naver API error for '%s': %s", query, exc)
                return []
            time.sleep(1.5)
    else:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles: list[dict] = []
    for item in data.get("items", []):
        try:
            pub = parsedate_to_datetime(item["pubDate"]).astimezone(timezone.utc)
            if pub < cutoff:
                continue
            articles.append({
                "title":   strip_html(item.get("title", "")),
                "link":    item.get("originallink") or item.get("link", ""),
                # ★ 표시는 원문 링크로 하되, 본문 파싱은 네이버 미러
                #   (n.news.naver.com)가 훨씬 안정적이라 따로 들고 다닌다.
                "naver_link": item.get("link", ""),
                "snippet": strip_html(item.get("description", ""))[:500],
            })
        except Exception:
            continue
    return articles


# ── LLM (공급자 교체 가능) ───────────────────────────────────────────────────
_last_llm = [0.0]


def _pace(interval: float) -> None:
    wait = interval - (time.monotonic() - _last_llm[0])
    if wait > 0:
        time.sleep(wait)
    _last_llm[0] = time.monotonic()


def llm_enabled() -> bool:
    cfg = PROVIDERS.get(LLM_PROVIDER)
    return bool(cfg and cfg["key"])


def _call_llm(prompt: str) -> Optional[str]:
    """공급자별 REST 호출. 실패하면 None → 호출부에서 스니펫 요약으로 대체.
    ★ 어떤 공급자를 쓰든 요청/응답 형태만 다르고 나머지 로직은 동일하다."""
    cfg = PROVIDERS.get(LLM_PROVIDER)
    if not cfg or not cfg["key"]:
        return None

    key, model = cfg["key"], cfg["model"]

    if LLM_PROVIDER == "gemini":
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
        # ★ maxOutputTokens 를 명시하지 않으면 모델 기본값에 끌려간다. thinkingBudget
        #   0 은 사고 토큰이 출력 예산을 잡아먹어 본문이 잘리는 것을 막는다.
        payload = {"contents": [{"parts": [{"text": prompt}]}],
                   "generationConfig": {"maxOutputTokens": LLM_MAX_TOKENS,
                                        "temperature": 0.2,
                                        "thinkingConfig": {"thinkingBudget": 0}}}
        extract = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]
        finish  = lambda d: (d.get("candidates") or [{}])[0].get("finishReason")

    elif LLM_PROVIDER == "groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": LLM_MAX_TOKENS,
                   "messages": [{"role": "user", "content": prompt}]}
        extract = lambda d: d["choices"][0]["message"]["content"]
        finish  = lambda d: (d.get("choices") or [{}])[0].get("finish_reason")

    elif LLM_PROVIDER == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": LLM_MAX_TOKENS,
                   "messages": [{"role": "user", "content": prompt}]}
        extract = lambda d: d["content"][0]["text"]
        finish  = lambda d: d.get("stop_reason")

    else:
        return None

    for attempt in range(3):
        _pace(cfg["interval"])
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code == 429:
                wait = 10 * (attempt + 1)
                logger.warning("%s 429 (한도) — %ds 대기 후 재시도", LLM_PROVIDER, wait)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                logger.error("%s 인증 실패 [%s] — API 키를 확인하세요",
                             LLM_PROVIDER, r.status_code)
                return None
            r.raise_for_status()
            data = r.json()
            # ★ 길이 한도로 끊겼는지를 로그에 남긴다. 이게 없으면 "요약이 잘린다"를
            #   다음에 또 맨눈으로 추적해야 한다.
            reason = finish(data)
            if reason and str(reason).lower() in ("length", "max_tokens"):
                logger.warning("%s 응답이 출력 토큰 한도(%d)에서 끊김 — "
                               "LLM_BATCH(%d) 를 줄이거나 LLM_MAX_TOKENS 를 올리세요",
                               LLM_PROVIDER, LLM_MAX_TOKENS, LLM_BATCH)
            return extract(data).strip()
        except Exception as exc:
            logger.warning("%s 호출 실패(%d/3): %s", LLM_PROVIDER, attempt + 1, exc)
            if attempt == 2:
                return None
            time.sleep(3)
    return None


def snippet_summary(snippet: str) -> str:
    """LLM 없이 스니펫만으로 만드는 대체 요약 — 한 줄.

    ★ 네이버 description 은 본문의 앞 200자 남짓을 잘라 '...' 을 붙여 준다.
      즉 마지막 조각은 거의 항상 문장 중간에서 끊긴 파편이다. 예전 코드는
      '...' 만 지우고 그 파편을 그대로 불릿으로 썼기 때문에 요약이 말이
      끊긴 채로 나갔다. 종결부호가 없는 꼬리는 버린다."""
    clean = re.sub(r"\s*(\.\.\.+|…)\s*$", "", snippet).strip()
    clean = re.sub(r"\s*(\.\.\.+|…)\s*", " ", clean).strip()

    # 한국어 종결('~다.', '~요.')과 일반 종결부호를 문장 경계로 본다.
    parts = [p.strip() for p in re.split(r"(?<=[.!?。])\s+", clean) if len(p.strip()) > 5]

    if parts:
        # 종결부호로 끝나는 첫 문장이 있으면 그것 하나만 쓴다.
        for p in parts:
            if re.search(r"[.!?。]$", p):
                return clip(p, 160)
        return clip(parts[0], 160) + " …"

    # 문장 경계를 못 찾은 경우(=스니펫 전체가 한 파편). 통째로 버리지 말고
    # 어절 경계에서 끊고 잘렸다는 표시를 남긴다 — 단어 중간에서 끊지 않는다.
    if not clean:
        return "(요약 없음)"
    out = clip(clean, 160)
    if not re.search(r"[.!?。]$", out):
        out += " …"          # 버그가 아니라 원문 발췌임을 명시
    return out


def clip(text: str, limit: int) -> str:
    """어절 경계에서만 자른다 — 단어/조사 중간에서 끊지 않는다."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,·-")


def parse_verdicts(raw: str) -> dict[int, dict]:
    """LLM 응답에서 판정 객체를 최대한 건져낸다.

    ★ 예전에는 json.loads 가 실패하면 배치 전체를 스니펫 요약으로 떨어뜨렸다.
      응답이 토큰 한도에서 끊기면 마지막 한 건 때문에 앞의 다섯 건까지 같이
      죽는 구조라, 브리프 전체가 '잘린 요약' 으로 보였다. 이제는 온전하게
      닫힌 객체만 골라 살리고, 정말 끊긴 마지막 객체만 폴백으로 보낸다."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    try:
        return {int(v["i"]): v for v in json.loads(raw) if "i" in v}
    except Exception:
        pass

    decoder = json.JSONDecoder()
    out: dict[int, dict] = {}
    pos = 0
    while True:
        start = raw.find("{", pos)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, start)
        except ValueError:
            pos = start + 1          # 여기서 끊긴 객체 — 다음 '{' 부터 다시
            continue
        if isinstance(obj, dict) and "i" in obj:
            try:
                out[int(obj["i"])] = obj
            except (TypeError, ValueError):
                pass
        pos = end
    return out


def one_line(value) -> str:
    """LLM 이 문자열 대신 리스트를 돌려주는 경우까지 흡수해 '한 줄'로 만든다.

    ★ 프롬프트를 1줄로 바꿔도 모델은 종종 예전처럼 배열을 돌려준다. 그때
      리스트를 그대로 렌더링하면 텔레그램에 ['...', '...'] 이 찍힌다."""
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        value = ", ".join(parts[:2])
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[-•·*]\s*", "", text)
    text = re.sub(r"\s*(\.\.\.+|…)\s*$", "", text).strip()
    return clip(text, SUMMARY_MAX_CHARS + 30)


ARTICLE_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 기사 본문이 들어있는 컨테이너. 위에서부터 시도하고 가장 긴 것을 쓴다.
# (네이버 뉴스 미러는 #dic_area / #newsct_article 로 통일돼 있어 가장 안정적이다)
ARTICLE_CONTAINERS = [
    r'<(?:div|article|section)[^>]+id="dic_area"[^>]*>(.*?)</(?:div|article|section)>',
    r'<(?:div|article|section)[^>]+id="newsct_article"[^>]*>(.*?)</(?:div|article|section)>',
    r'<(?:div|article|section)[^>]+id="articleBodyContents"[^>]*>(.*?)</(?:div|article|section)>',
    r'<(?:div|article|section)[^>]+id="article-view-content-div"[^>]*>(.*?)</(?:div|article|section)>',
    r'<(?:div|article|section)[^>]+itemprop="articleBody"[^>]*>(.*?)</(?:div|article|section)>',
    r'<(?:div|article|section)[^>]+class="[^"]*'
    r'(?:news_cnt_detail_wrap|article_view|article-body|story-news|news-article-body)'
    r'[^"]*"[^>]*>(.*?)</(?:div|article|section)>',
    r'<article[^>]*>(.*?)</article>',
]

# ★ 삭제·이전된 기사인데도 200 OK 로 안내 페이지를 돌려주는 매체가 있다.
#   그 안내문이 '완전한 문장'이라 그대로 리드로 실려 나가므로 걸러낸다.
ERROR_PAGE_RE = re.compile(r"페이지를\s*찾을\s*수\s*없|존재하지\s*않는\s*(페이지|기사)|"
                           r"삭제된\s*기사|잘못된\s*접근|Page\s*Not\s*Found|"
                           r"서비스\s*점검\s*중")

# 사진 캡션·저작권 고지는 문장처럼 생겼지만 기사가 아니다.
JUNK_SENT_RE = re.compile(r"사진\s*=|제공\s*=|자료\s*=|ⓒ|©|무단\s*전재|재배포\s*금지|"
                          r"@[\w.]+|기자\s*$|뉴스1$|연합뉴스$")


def _clean_lead(text: str) -> str:
    """통신사 머리표·바이라인을 걷어낸다.
    '[서울=뉴시스] 김철수 기자 = 현대차가...' 처럼 본문 앞에 붙는 것들."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^[\[\(【<][^\]\)】>]{0,40}[\]\)】>]\s*", "", text)
    m = re.match(r"^.{0,45}?기자\s*=\s*", text)
    if m:
        text = text[m.end():]
    return text.strip()


def fetch_article_text(url: str) -> str:
    """기사 페이지를 받아 본문 텍스트를 뽑는다. 실패하면 빈 문자열."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=ARTICLE_UA, timeout=LEAD_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        logger.debug("본문 fetch 실패 %s: %s", url[:60], exc)
        return ""

    # ★ charset 헤더가 없으면 requests 는 ISO-8859-1 로 가정한다. 국내 언론사는
    #   아직 euc-kr 이 남아 있어서, 이걸 안 고치면 본문이 통째로 깨진다.
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding or "utf-8"
    page = r.text

    page = re.sub(r"(?is)<(script|style|figcaption|table|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?is)<figure[^>]*>.*?</figure>", " ", page)
    page = re.sub(r"(?is)<br\s*/?>", " ", page)

    # ★ 후보를 모두 모아 점수로 고른다. 본문 컨테이너는 언론사마다 제각각이라
    #   한 패턴만 믿으면 사이트가 바뀌는 순간 조용히 스니펫으로 떨어진다.
    #   og:description 은 거의 모든 매체가 리드 문단을 그대로 넣어 두므로
    #   가장 든든한 예비 소스다 — 다만 200자쯤에서 잘리는 곳이 있다.
    cands = []
    for pat in ARTICLE_CONTAINERS:
        m = re.search(pat, page, re.S | re.I)
        if m:
            cands.append(_clean_lead(strip_html(m.group(1))))
    m = re.search(r'<meta[^>]+(?:property|name)=["\']og:description["\'][^>]*'
                  r'content=["\']([^"\']+)', page, re.I)
    if m:
        cands.append(_clean_lead(strip_html(m.group(1))))

    # 완전한 문장을 LEAD_SENTENCES 개 뽑을 수 있는지가 1순위, 길이가 2순위.
    def _score(t: str) -> tuple:
        return (min(len(first_sentences(t, LEAD_SENTENCES)), LEAD_SENTENCES), len(t))

    cands = [c for c in cands if c and not ERROR_PAGE_RE.search(c[:200])]
    return max(cands, key=_score) if cands else ""


def first_sentences(text: str, n: int) -> list[str]:
    """종결부호로 끝나는 완전한 문장만 앞에서부터 n 개.

    ★ 잘린 꼬리(종결부호가 없는 조각)는 버린다 — '요약이 문장 중간에서
      끊긴다'는 민원의 실체가 바로 이 파편이었다."""
    if not text:
        return []
    out: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", text):
        part = part.strip()
        if len(part) < 10 or not re.search(r"[.!?]$", part):
            continue
        if JUNK_SENT_RE.search(part):
            continue
        out.append(clip(part, LEAD_MAX_CHARS))
        if len(out) >= n:
            break
    return out


def article_lead(a: dict) -> list[str]:
    """기사 본문의 첫 LEAD_SENTENCES 문장을 '원문 그대로' 돌려준다."""
    text = ""
    if LEAD_FETCH:
        tried: list[str] = []
        for url in (a.get("naver_link"), a.get("link")):
            if not url or url in tried:
                continue
            tried.append(url)
            text = fetch_article_text(url)
            if len(text) >= 80:
                break
            time.sleep(0.2)

    lines = first_sentences(text, LEAD_SENTENCES)
    if not lines:
        # 본문을 못 읽었을 때만 네이버 스니펫으로 후퇴한다(대개 한 문장).
        lines = first_sentences(_clean_lead(a.get("snippet", "")), LEAD_SENTENCES)
    if not lines:
        lines = [snippet_summary(a.get("snippet", ""))]
    return lines


def flat_text(value) -> str:
    """모델이 리스트/줄바꿈으로 돌려줘도 한 줄로 만든다(길이는 건드리지 않는다)."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v).strip() for v in value if str(v).strip())
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"^[-•·*]\s*", "", text).strip()


def judge_relevance(articles: list[dict], context: str) -> list[dict]:
    """★ 여러 건을 한 번의 호출로 '팔로업할 가치가 있는가'만 판정한다.
    요약은 더 이상 모델이 쓰지 않는다(본문 리드를 그대로 쓴다).
    호출 실패 시 전부 통과시킨다 — 기사 유실보다 노이즈가 낫다."""
    kept: list[dict] = []

    for i in range(0, len(articles), LLM_BATCH):
        batch = articles[i:i + LLM_BATCH]

        listing = "\n\n".join(
            f"[{n}]\n제목: {a['title']}\n내용: {a['snippet']}"
            for n, a in enumerate(batch)
        )
        prompt = (
            f"당신은 {context}를 커버하는 한국 주식 애널리스트입니다.\n"
            "아래 기사 각각에 대해 '팔로업할 가치가 있는 펀더멘털 뉴스인가'를 "
            "판단하세요.\n"
            "   true  = 실적/가이던스, 수주·계약, 생산·수출, 신제품·기술·R&D, "
            "설비투자·증설, M&A·지분, 업황·전방수요, 규제·정책, 경영전략·인사, 소송·리스크\n"
            "   false = 단순 주가 등락, 증권사 목표주가 전달, 시황 기사의 종목 나열, "
            "홍보성·사회공헌·재단/교육 프로그램·스포츠 스폰서, 동명 기업 오인, "
            "내용 없는 공시 알림\n\n"
            "출력은 아래 형식의 JSON 배열만. 다른 텍스트는 절대 쓰지 마세요.\n"
            '[{"i":0,"keep":true},{"i":1,"keep":false}]\n\n'
            f"기사 목록:\n{listing}"
        )

        raw = _call_llm(prompt)
        if raw is None:
            logger.info("LLM 사용 불가 — 규칙 필터만으로 통과 (%d건)", len(batch))
            kept.extend(batch)
            continue

        by_index = parse_verdicts(raw)
        if not by_index:
            logger.warning("판정 JSON 파싱 실패 (응답 %d자) — 이 배치는 그대로 통과",
                           len(raw))
            kept.extend(batch)
            continue

        for n, a in enumerate(batch):
            v = by_index.get(n)
            if v is None:
                kept.append(a)          # 판정을 못 받았으면 살린다
                continue
            if not v.get("keep"):
                logger.info("SKIP (무관): %s", a["title"][:55])
                continue
            kept.append(a)

    return kept


def translate_articles(articles: list[dict]) -> None:
    """제목과 리드 문장을 영어로 '번역'한다 — 요약이 아니다.

    ★ 번역에 실패한 배치는 한국어 원문 그대로 나간다. 기사를 잃지 않는 것이
      영어로 나가는 것보다 우선한다."""
    for a in articles:
        a["title_en"] = ""
        a["lead_en"] = []

    if BRIEF_LANG == "ko":
        return
    if not llm_enabled():
        logger.warning("LLM 비활성 — 번역 없이 한국어 원문으로 발송합니다.")
        return

    done = 0
    for i in range(0, len(articles), TRANSLATE_BATCH):
        batch = articles[i:i + TRANSLATE_BATCH]
        listing = "\n\n".join(
            "[{}] company: {}\nheadline: {}\nbody:\n{}".format(
                n, a.get("company", ""), a["title"],
                "\n".join(f"  ({j + 1}) {t}" for j, t in enumerate(a["lead_ko"])))
            for n, a in enumerate(batch)
        )
        prompt = (
            "You are translating Korean news items for an equity-research morning "
            "brief read by professional investors.\n\n"
            "Rules:\n"
            "- Translate literally. Do NOT summarise, shorten, merge, reorder or omit "
            "anything, and do not add anything that is not in the Korean source.\n"
            "- Return exactly one English sentence for each numbered source sentence, "
            "in the same order.\n"
            "- Keep every number, unit, percentage, date and ratio exactly as written.\n"
            "- Use the company's standard English name; the covered company is given "
            "as 'company' for each item.\n"
            "- Complete, plain declarative sentences ending in a period. No bullets, "
            "no markdown, no ellipsis, no truncation.\n"
            "- Translate the headline as a headline: no trailing period, keep any "
            "quoted phrase in quotes.\n\n"
            "Output a JSON array only, nothing else:\n"
            '[{"i":0,"headline":"...","body":["...","..."]}]\n\n'
            f"Items:\n{listing}"
        )

        raw = _call_llm(prompt)
        if raw is None:
            logger.warning("번역 호출 실패 — 이 배치 %d건은 한국어로 나갑니다", len(batch))
            continue

        by_index = parse_verdicts(raw)
        if not by_index:
            logger.warning("번역 JSON 파싱 실패 (응답 %d자) — 이 배치 %d건은 한국어로",
                           len(raw), len(batch))
            continue

        for n, a in enumerate(batch):
            v = by_index.get(n)
            if not v:
                continue
            body = v.get("body")
            if isinstance(body, str):
                body = [body]
            lines = [flat_text(t) for t in (body or [])]
            lines = [t for t in lines if t][:LEAD_SENTENCES]
            head = flat_text(v.get("headline"))
            if head:
                a["title_en"] = head
            if lines:
                a["lead_en"] = lines
                done += 1

    if done < len(articles):
        logger.warning("번역 %d/%d건 — 나머지는 한국어 원문", done, len(articles))


def render_block(a: dict) -> str:
    """■ 제목 / 링크 / 리드 문장. 요약이 아니라 본문 발췌다."""
    use_en = BRIEF_LANG != "ko" and (a.get("title_en") or a.get("lead_en"))
    title = (a.get("title_en") or a["title"]) if use_en else a["title"]
    lead = (a.get("lead_en") or a.get("lead_ko") or []) if use_en else a.get("lead_ko", [])

    lines = [f"■ <b>{escape_html(title)}</b>", escape_html(a["link"])]
    lines += [f"- {escape_html(t)}" for t in lead] or ["- (본문 없음)"]
    if BRIEF_LANG == "both" and use_en:
        lines.append(f"<i>{escape_html(a['title'])}</i>")
    return "\n".join(lines) + "\n"


# ── Telegram ─────────────────────────────────────────────────────────────────
DRY_RUN = "--dry-run" in sys.argv


def tg_send(text: str) -> bool:
    """★ 실패를 조용히 삼키지 않는다 — 어떤 오류인지 로그에 남기고 False 반환."""
    if DRY_RUN:
        print("\n" + "-" * 60 + "\n" + text)
        return True
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=20)
    except Exception as exc:
        logger.error("텔레그램 네트워크 오류: %s", exc)
        return False

    if r.status_code != 200:
        logger.error("텔레그램 발송 실패 [HTTP %s]: %s", r.status_code, r.text[:300])
        if r.status_code == 401:
            logger.error("→ TELEGRAM_BOT_TOKEN 이 잘못되었습니다.")
        elif r.status_code == 400 and "chat not found" in r.text.lower():
            logger.error("→ TELEGRAM_CHAT_ID 가 잘못되었습니다. 봇에게 먼저 /start 를 보내세요.")
        elif r.status_code == 403:
            logger.error("→ 봇이 차단되었거나 그룹에서 제거되었습니다.")
        return False

    time.sleep(0.5)
    return True


TITLE_RE = re.compile(r"^(■ )?<b>(.*)</b>$", re.S)


def split_long_line(line: str, limit: int) -> list[str]:
    """한도를 넘는 '한 줄'을 태그를 깨지 않고 나눈다.

    ★ 예전 코드는 line[i:i+limit] 로 문자 단위로 그냥 잘랐다. 제목 줄은
      '■ <b>제목</b>' 이라 태그 안쪽에서 잘리면 <b> 가 열린 채로 나가고,
      텔레그램이 400 (can't parse entities) 을 돌려주며 그 메시지가 통째로
      사라졌다. 즉 '제목이 잘린' 게 아니라 '기사가 통째로 증발한' 것이다.
      이제 제목은 <b>…</b> 를 조각마다 다시 닫아 주고, 어절 경계에서만 나눈다."""
    m = TITLE_RE.match(line)
    marker, inner = (m.group(1) or "", m.group(2)) if m else ("", line)
    overhead = len(marker) + (7 if m else 0)       # '<b>' + '</b>'
    budget = max(20, limit - overhead)

    pieces, cur = [], ""
    for word in inner.split(" "):
        while len(word) > budget:                  # 공백 없는 초장문(URL 등)
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(word[:budget])
            word = word[budget:]
        if cur and len(cur) + 1 + len(word) > budget:
            pieces.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        pieces.append(cur)

    if not m:
        return pieces
    # 첫 조각에만 '■', 모든 조각은 <b> 로 열고 닫는다 → 태그가 절대 안 깨진다.
    return [f"{marker if i == 0 else ''}<b>{p}</b>" for i, p in enumerate(pieces)]


def split_block(block: str, limit: int) -> list[str]:
    """기사 한 건이 혼자서도 한도를 넘을 때만 쓰는 줄 단위 분할.
    줄 경계에서만 나누고, 어쩔 수 없이 한 줄을 나눠야 하면 split_long_line 이
    태그·어절을 지켜 나눈다."""
    if len(block) <= limit:
        return [block]

    out, cur = [], ""
    for line in block.split("\n"):
        for piece in (split_long_line(line, limit) if len(line) > limit else [line]):
            if cur and len(cur) + len(piece) + 1 > limit:
                out.append(cur.rstrip("\n"))
                cur = ""
            cur += piece + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out


def split_into_messages(header: str, blocks: list[str],
                        cont_header: str = "") -> list[str]:
    """기사(block) 경계에서만 메시지를 나눈다. 기사 중간에서는 절대 자르지 않는다.

    ★ cont_header 는 2번째 이후 메시지 앞에 붙는 '(계속)' 머리말. 예전에는
      분할이 끝난 뒤에 붙였기 때문에 그만큼 한도를 초과할 수 있었다. 이제
      예산에 미리 반영한다."""
    limit = TELEGRAM_MAX_CHARS
    body_limit = limit - len(cont_header) - 4

    messages: list[str] = []
    current = header + "\n\n" if header else ""

    for block in blocks:
        for piece in split_block(block, body_limit):
            piece = piece + "\n"
            if current.strip() and len(current) + len(piece) > (
                    limit if not messages else body_limit):
                messages.append(current.rstrip())
                current = piece
            else:
                current += piece
    if current.strip():
        messages.append(current.rstrip())
    return messages


# ── Collection ───────────────────────────────────────────────────────────────
def collect(entries, hours, seen, seen_titles, accepted) -> list[dict]:
    priority: list[dict] = []
    normal: list[dict] = []
    st = {"fetched": 0, "noise": 0, "no_kw": 0, "seen": 0, "dup": 0, "kept": 0}

    # entries 튜플은 (표시명, 검색어, 키워드) 로 통일되어 있다 — 국내/해외 구분 없음
    for label, query, keywords in entries:
        count = 0

        for art in fetch_naver_news(query, hours):
            st["fetched"] += 1
            if MAX_PER_COMPANY is not None and count >= MAX_PER_COMPANY:
                break

            if not title_has_company(art["title"], keywords):
                st["no_kw"] += 1
                continue
            if is_noise(art["title"]):
                st["noise"] += 1
                continue

            link = art["link"]
            if link in seen:
                st["seen"] += 1
                continue

            key = art["title"][:60].lower()
            if key in seen_titles or is_content_duplicate(art, accepted):
                st["dup"] += 1
                seen_titles.add(key)
                continue

            seen_titles.add(key)
            accepted.append(art)
            count += 1
            st["kept"] += 1
            art["company"] = label
            (priority if title_has_company(art["title"], keywords) else normal).append(art)

    logger.info("  조회 %d → 키워드밖 %d / 노이즈 %d / 기발송 %d / 중복 %d → 후보 %d",
                st["fetched"], st["no_kw"], st["noise"], st["seen"], st["dup"], st["kept"])
    return priority + normal


def run_sector(title: str, spec: dict, hours, seen, seen_titles, accepted) -> int:
    logger.info("[%s]", title)
    candidates = collect(spec["entries"], hours, seen, seen_titles, accepted)
    if not candidates:
        return 0

    articles = judge_relevance(candidates, spec["context"])
    if not articles:
        logger.info("  → LLM 필터 후 남은 기사 없음")
        return 0

    # ★ 요약하지 않는다. 본문 첫 두 문장을 그대로 가져와 번역만 한다.
    #   (본문 fetch 는 판정을 통과한 기사에 대해서만 — 버려질 기사를 받지 않는다)
    for a in articles:
        a["lead_ko"] = article_lead(a)
    translate_articles(articles)

    blocks = [render_block(a) for a in articles]
    now_iso = now_kst().isoformat()
    for a in articles:
        seen[a["link"]] = now_iso

    cont_header = f"<b>{escape_html(title)} (계속)</b>\n\n"
    chunks = split_into_messages(f"<b>{escape_html(title)}</b>", blocks, cont_header)
    failed = 0
    for i, msg in enumerate(chunks):
        if i > 0:
            msg = cont_header + msg.lstrip()
        if not tg_send(msg):
            failed += 1

    # ★ 실패를 조용히 넘기면 '기사가 중간에서 사라진' 것처럼 보인다.
    if failed:
        logger.error("  → %d/%d 메시지 발송 실패 (해당 기사들이 누락됨)",
                     failed, len(chunks))
    logger.info("  → %d건 발송 (메시지 %d개)", len(articles), len(chunks))
    return len(articles)


# ── Main ─────────────────────────────────────────────────────────────────────
def parse_group_arg() -> list[str]:
    """--group auto | other | all  (기본 all). 환경변수 NEWS_GROUP 도 지원."""
    value = os.getenv("NEWS_GROUP", "all").strip().lower()
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--group" and i + 1 < len(argv):
            value = argv[i + 1].strip().lower()
        elif a.startswith("--group="):
            value = a.split("=", 1)[1].strip().lower()

    if value in ("all", ""):
        return list(GROUPS)
    if value in GROUPS:
        return [value]
    logger.error("알 수 없는 --group '%s' — 사용 가능: %s | all",
                 value, " | ".join(GROUPS))
    sys.exit(2)


def run_brief(group: str, hours: int, label: str, now: datetime) -> int:
    """그룹 하나(auto / other)를 처음부터 끝까지 — seen 로드·헤더·발송·저장."""
    spec = GROUPS[group]
    path = seen_path(group)
    seen = load_seen(path)
    logger.info("=== [%s] 시작 — %s | 창: %s | %s 기존 %d건 ===",
                group, now.strftime("%Y-%m-%d %H:%M KST"), label, path, len(seen))

    header = (f"<b>{escape_html(spec['title'])}</b>\n"
              f"<i>{now.strftime('%Y.%m.%d %H:%M KST')} · {label}</i>\n"
              f"{'─' * 30}")
    if not tg_send(header):
        logger.error("헤더 발송 실패 — 텔레그램 설정을 확인하세요. [%s] 중단합니다.", group)
        return -1

    # ★ seen_titles / accepted 는 그룹 안에서만 공유한다. 그룹 간에 섞으면
    #   auto 잡과 other 잡을 따로 돌릴 때와 결과가 달라진다.
    seen_titles: set[str] = set()
    accepted: list[dict] = []
    total = 0
    for sector, sector_spec in spec["coverage"].items():
        total += run_sector(sector, sector_spec, hours, seen, seen_titles, accepted)

    if total == 0:
        tg_send("<i>해당 윈도우 내 새 기사 없음</i>")

    save_seen(path, seen)
    logger.info("=== [%s] 완료 — 총 %d건 발송 ===", group, total)
    return total


def main() -> None:
    required = {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID,
                "NAVER_CLIENT_ID": NAVER_CLIENT_ID,
                "NAVER_CLIENT_SECRET": NAVER_CLIENT_SECRET}
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("환경변수 누락: %s", ", ".join(missing))
        logger.error("→ .env 파일 또는 GitHub Secrets 를 확인하세요. (python check.py)")
        sys.exit(1)

    if llm_enabled():
        logger.info("LLM 공급자: %s (%s)", LLM_PROVIDER,
                    PROVIDERS[LLM_PROVIDER]["model"])
    else:
        logger.warning("LLM 비활성(provider=%s) — 관련성 판정과 영어 번역 없이 "
                       "한국어 원문으로 발송됩니다.", LLM_PROVIDER)

    force = "--now" in sys.argv
    now = now_kst()

    # ★ 주말 게이트는 --now 여도 존중한다. --now 가 무시하는 것은 '시간대'뿐.
    #   (Actions 는 항상 --now 로 도니, 여기서 빼면 SKIP_WEEKEND 가 무력해진다)
    if SKIP_WEEKEND and now.weekday() >= 5:
        logger.info("주말이므로 실행하지 않습니다.")
        return

    if not force:
        if now.hour not in SEND_HOURS:
            logger.info("발송 시간대가 아닙니다 (현재 %s KST, 허용 %s시)",
                        now.strftime("%H:%M"), sorted(SEND_HOURS))
            return

    hours = news_window_hours()
    label = "72h (Mon)" if hours == 72 else "24h"
    groups = parse_group_arg()
    logger.info("실행 그룹: %s | 언어: %s", ", ".join(groups), BRIEF_LANG)

    failed = False
    for group in groups:
        if run_brief(group, hours, label, now) < 0:
            failed = True

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
