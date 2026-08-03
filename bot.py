#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Equity Research News Bot — Naver News + LLM (공급자 교체 가능)
- 한국 커버리지 + 글로벌/일본 Auto peers, 네이버 일반 검색
- 요약: Gemini / Groq / Anthropic 중 택1 (LLM_PROVIDER 환경변수).
  키가 없거나 한도 초과 시 스니펫 요약으로 자동 대체되므로 봇이 멈추지 않음
- GitHub Actions 에서 1회성으로 실행되는 구조 (상주 프로세스 아님)

- 매일 06:00 KST 1회 발송. 스케줄은 .github/workflows/news-run.yml 의 cron 이 쥔다.

실행:
    python bot.py            # SEND_HOURS 시간대일 때만 발송
    python bot.py --now      # 시간대 무시하고 즉시 1회 발송 (Actions / 테스트용)
    python bot.py --dry-run  # 텔레그램 발송 없이 콘솔에만 출력

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

SEEN_FILE      = "seen.json"   # 실행 간 중복 방지 (GitHub Actions 캐시로 보존)
SEEN_KEEP_DAYS = 14

# ── LLM 설정 ────────────────────────────────────────────────────────────────
# LLM_PROVIDER 환경변수로 교체:  gemini | groq | anthropic | none
#   gemini    : 무료. GEMINI_API_KEY      (aistudio.google.com)
#   groq      : 무료 티어가 가장 넉넉함. GROQ_API_KEY   (console.groq.com)
#   anthropic : 유료지만 배치 처리 시 월 $2~3 수준. ANTHROPIC_API_KEY
#   none      : LLM 없이 스니펫 요약만 사용 (완전 무료, 품질 낮음)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

LLM_BATCH = 10    # 한 번의 호출에 넣을 기사 수. 호출 수를 1/10 로 줄이는 핵심.

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
COVERAGE: dict[str, list[tuple[str, str, list[str]]]] = {
    "Auto": [
        ("현대자동차", "Hyundai Motor", ["현대자동차", "현대차"]),
        ("기아",       "Kia",           ["기아"]),
        ("현대모비스", "Hyundai Mobis", ["현대모비스"]),
        ("HL만도",     "HL Mando",      ["HL만도", "만도"]),
        ("한국타이어", "Hankook Tire",  ["한국타이어", "한국앤컴퍼니"]),
        ("한온시스템", "Hanon Systems", ["한온시스템"]),
    ],
    "EV / Battery": [
        ("LG에너지솔루션", "LG Energy Solution", ["LG에너지솔루션", "LG엔솔", "LGES"]),
        ("삼성SDI",        "Samsung SDI",        ["삼성SDI"]),
        ("SK이노베이션",   "SK Innovation",      ["SK이노베이션", "SK온"]),
        ("포스코퓨처엠",   "POSCO Future M",     ["포스코퓨처엠"]),
        ("엘앤에프",       "L&F",                ["엘앤에프", "L&F"]),
    ],
    "Construction": [
        ("현대건설", "Hyundai E&C", ["현대건설"]),
        ("GS건설",   "GS E&C",      ["GS건설"]),
        ("삼성E&A",  "Samsung E&A", ["삼성E&A", "삼성엔지니어링"]),
        ("삼성물산", "Samsung C&T", ["삼성물산"]),
    ],
    "Shipbuilding": [
        ("한화오션",       "Hanwha Ocean",    ["한화오션"]),
        ("삼성중공업",     "Samsung Heavy",   ["삼성중공업"]),
        ("HD현대중공업",   "HD Hyundai HI",   ["HD현대중공업", "현대중공업"]),
        ("한화엔진",       "Hanwha Engine",   ["한화엔진"]),
        ("HD한국조선해양", "HD Hyundai KSOE", ["HD한국조선해양", "한국조선해양", "KSOE"]),
    ],
}

AUTO_PEERS: dict[str, list[tuple[str, str, list[str]]]] = {
    "Global Peers": [
        ("Tesla",      "테슬라",       ["테슬라"]),
        ("BMW",        "BMW",          ["BMW"]),
        ("Volkswagen", "폭스바겐",     ["폭스바겐", "폴크스바겐"]),
        ("GM",         "제너럴모터스", ["제너럴모터스", "GM"]),
        ("Stellantis", "스텔란티스",   ["스텔란티스"]),
    ],
    "Japanese Peers": [
        ("Toyota", "도요타", ["도요타", "토요타"]),
        ("Honda",  "혼다",   ["혼다"]),
        ("Nissan", "닛산",   ["닛산"]),
    ],
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
    r"우수기업\s*선정|대상\s*수상|공모전"
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
def load_seen() -> dict:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    cutoff = (now_kst() - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=0)
        logger.info("seen.json 저장 — %d건 보관", len(seen))
    except Exception as exc:
        logger.warning("seen.json 저장 실패: %s", exc)


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
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        extract = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]

    elif LLM_PROVIDER == "groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": 2000,
                   "messages": [{"role": "user", "content": prompt}]}
        extract = lambda d: d["choices"][0]["message"]["content"]

    elif LLM_PROVIDER == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        payload = {"model": model, "max_tokens": 2000,
                   "messages": [{"role": "user", "content": prompt}]}
        extract = lambda d: d["content"][0]["text"]

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
            return extract(r.json()).strip()
        except Exception as exc:
            logger.warning("%s 호출 실패(%d/3): %s", LLM_PROVIDER, attempt + 1, exc)
            if attempt == 2:
                return None
            time.sleep(3)
    return None


def snippet_summary(snippet: str) -> str:
    """LLM 없이 스니펫만으로 만드는 대체 요약."""
    clean = re.sub(r"\s*(\.\.\.+|…)\s*", " ", snippet).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?。])\s+", clean) if len(p.strip()) > 5]
    parts = parts[:2] or [clean or "(요약 없음)"]
    return "\n".join(f"- {p}" for p in parts)


def judge_and_summarise(articles: list[dict], context: str) -> list[dict]:
    """★ 기사 여러 건을 한 번의 호출로 [관련성 판정 + 2불릿 요약] 처리.
    무료 티어에서 호출 수를 LLM_BATCH 배로 줄이는 것이 목적.
    호출 실패 시 전부 통과시키고 스니펫 요약으로 대체한다(기사 유실 방지)."""
    kept: list[dict] = []

    for i in range(0, len(articles), LLM_BATCH):
        batch = articles[i:i + LLM_BATCH]

        listing = "\n\n".join(
            f"[{n}]\n제목: {a['title']}\n내용: {a['snippet']}"
            for n, a in enumerate(batch)
        )
        prompt = (
            f"당신은 {context}를 커버하는 한국 주식 애널리스트입니다.\n"
            "아래 기사 각각에 대해 두 가지를 판단하세요.\n\n"
            "1) keep: 해당 기업을 '팔로업'할 가치가 있는 펀더멘털 뉴스인가?\n"
            "   true  = 실적/가이던스, 수주·계약, 생산·수출, 신제품·기술·R&D, "
            "설비투자·증설, M&A·지분, 업황·전방수요, 규제·정책, 경영전략·인사, 소송·리스크\n"
            "   false = 단순 주가 등락, 증권사 목표주가 전달, 시황 기사의 종목 나열, "
            "홍보성·사회공헌·스포츠 스폰서, 동명 기업 오인, 내용 없는 공시 알림\n"
            "2) summary: keep이 true인 경우에만, 핵심 포인트 정확히 2개.\n"
            "   각 항목은 완전한 문장이 아닌 간결한 요약체. "
            "예: '2Q 영업이익 30% YoY 증가, 시장 예상치 상회'\n"
            "   원문에서 영어로 표기된 고유명사는 영어 그대로 유지.\n\n"
            "출력은 아래 형식의 JSON 배열만. 다른 텍스트는 절대 쓰지 마세요.\n"
            '[{"i":0,"keep":true,"summary":["...","..."]},'
            '{"i":1,"keep":false,"summary":[]}]\n\n'
            f"기사 목록:\n{listing}"
        )

        raw = _call_llm(prompt)
        if raw is None:
            # LLM 사용 불가 → 규칙 필터를 통과한 기사이므로 그대로 살린다
            logger.info("LLM 사용 불가 — 스니펫 요약으로 대체 (%d건)", len(batch))
            for a in batch:
                a["summary"] = snippet_summary(a["snippet"])
                kept.append(a)
            continue

        # ```json ... ``` 코드펜스 제거
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            verdicts = json.loads(raw)
            by_index = {int(v["i"]): v for v in verdicts if "i" in v}
        except Exception as exc:
            logger.warning("LLM JSON 파싱 실패(%s) — 스니펫 요약으로 대체", exc)
            for a in batch:
                a["summary"] = snippet_summary(a["snippet"])
                kept.append(a)
            continue

        for n, a in enumerate(batch):
            v = by_index.get(n)
            if v is None:
                a["summary"] = snippet_summary(a["snippet"])
                kept.append(a)
                continue
            if not v.get("keep"):
                logger.info("SKIP (무관): %s", a["title"][:55])
                continue
            bullets = [b.strip() for b in (v.get("summary") or []) if b.strip()][:2]
            a["summary"] = ("\n".join(f"- {b}" for b in bullets)
                            if bullets else snippet_summary(a["snippet"]))
            kept.append(a)

    return kept


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


def split_into_messages(header: str, blocks: list[str]) -> list[str]:
    messages: list[str] = []
    current = header + "\n\n"
    for block in blocks:
        if len(block) > TELEGRAM_MAX_CHARS:
            block = block[:TELEGRAM_MAX_CHARS - 20] + "\n…(생략)\n"
        if len(current) + len(block) > TELEGRAM_MAX_CHARS and current.strip():
            messages.append(current.rstrip())
            current = block + "\n"
        else:
            current += block + "\n"
    if current.strip():
        messages.append(current.rstrip())
    return messages


# ── Collection ───────────────────────────────────────────────────────────────
def collect(entries, hours, seen, seen_titles, accepted, peer: bool) -> list[dict]:
    priority: list[dict] = []
    normal: list[dict] = []
    st = {"fetched": 0, "noise": 0, "no_kw": 0, "seen": 0, "dup": 0, "kept": 0}

    for name_a, name_b, keywords in entries:
        query = name_b if peer else name_a
        label = name_a if peer else name_b
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


def run_group(title: str, entries, hours, seen, seen_titles, accepted,
              peer: bool = False) -> int:
    logger.info("[%s]", title)
    candidates = collect(entries, hours, seen, seen_titles, accepted, peer)
    if not candidates:
        return 0

    context = ("한국 자동차 섹터(현대차·기아 등) 대비 글로벌 peer 경쟁구도"
               if peer else f"한국 주식 {title} 섹터")
    articles = judge_and_summarise(candidates, context)
    if not articles:
        logger.info("  → LLM 필터 후 남은 기사 없음")
        return 0

    blocks = [
        f"■ <b>{escape_html(a['title'])}</b>\n"
        f"{escape_html(a['link'])}\n"
        f"{escape_html(a['summary'])}\n"
        for a in articles
    ]
    now_iso = now_kst().isoformat()
    for a in articles:
        seen[a["link"]] = now_iso

    for i, msg in enumerate(split_into_messages(f"<b>{escape_html(title)}</b>", blocks)):
        if i > 0:
            msg = f"<b>{escape_html(title)} (계속)</b>\n\n" + msg.lstrip()
        tg_send(msg)

    logger.info("  → %d건 발송", len(articles))
    return len(articles)


# ── Main ─────────────────────────────────────────────────────────────────────
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
        logger.warning("LLM 비활성(provider=%s) — 스니펫 기반 요약으로 동작합니다.", LLM_PROVIDER)

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
    logger.info("=== 뉴스런 시작 — %s | 창: %s ===", now.strftime("%Y-%m-%d %H:%M KST"), label)

    seen = load_seen()
    logger.info("seen.json — 기존 %d건 로드", len(seen))

    header = (f"<b>Equity Research News Brief</b>\n"
              f"<i>{now.strftime('%Y.%m.%d %H:%M KST')} · {label}</i>\n"
              f"{'─' * 30}")
    if not tg_send(header):
        logger.error("헤더 발송 실패 — 텔레그램 설정을 확인하세요. 중단합니다.")
        sys.exit(1)

    seen_titles: set[str] = set()
    accepted: list[dict] = []
    total = 0

    for sector, companies in COVERAGE.items():
        total += run_group(sector, companies, hours, seen, seen_titles, accepted)

    for group, peers in AUTO_PEERS.items():
        total += run_group(f"Auto Peers — {group}", peers, hours, seen,
                           seen_titles, accepted, peer=True)

    if total == 0:
        tg_send("<i>해당 윈도우 내 새 기사 없음</i>")

    save_seen(seen)
    logger.info("=== 완료 — 총 %d건 발송 ===", total)


if __name__ == "__main__":
    main()
