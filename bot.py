#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Equity Research News Bot — Naver News edition
- 한국 커버리지: 네이버 일반 검색
- 글로벌/일본 Auto peers: 네이버 일반 검색 (한국어 매체 기준)
섹터별로 묶어 06:00 / 13:00 / 18:00 KST 발송.
월요일 72h | 화~일 24h 조회.

실행:
    python bot.py          # 스케줄러 상주 (기동 시 1회 즉시 발송)
    python bot.py --now    # 1회만 발송하고 종료 (테스트용)
"""

import os
import re
import sys
import html
import asyncio
import logging
import requests
import anthropic

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN           = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID             = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
NAVER_CLIENT_ID     = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
KST                 = pytz.timezone("Asia/Seoul")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_MAX_CHARS = 3800  # 텔레그램 한도 4096에서 여유 확보
MAX_PER_COMPANY    = 3     # 회사(peer)당 최대 기사 수. None이면 무제한

# ★ Jaccard 유사도 임계값 — 이 값 이상이면 '같은 내용'으로 보고 버린다.
#   주의: 이 값을 너무 낮추면(예: 0.05) 서로 무관한 기사끼리도 조사·상투어가 겹쳐
#   전부 중복 처리되어 브리프가 비어버린다. 0.4~0.6 사이가 안전.
DEDUP_THRESHOLD    = 0.45
DEDUP_MIN_SHARED   = 5     # 유사도와 별개로, 공통 단어가 이 개수 미만이면 중복 아님

MODEL = "claude-haiku-4-5-20251001"

# ── Coverage universe (네이버 일반 검색) ──────────────────────────────────────
COVERAGE: dict[str, list[tuple[str, str, list[str]]]] = {
    "Auto": [
        ("현대자동차", "Hyundai Motor",   ["현대자동차", "현대차"]),
        ("기아",       "Kia",             ["기아"]),
        ("현대모비스", "Hyundai Mobis",   ["현대모비스"]),
        ("HL만도",     "HL Mando",        ["HL만도", "만도"]),
        ("한국타이어", "Hankook Tire",    ["한국타이어", "한국앤컴퍼니"]),
        ("한온시스템", "Hanon Systems",   ["한온시스템"]),
    ],
    "EV / Battery": [
        ("LG에너지솔루션", "LG Energy Solution", ["LG에너지솔루션", "LG엔솔", "LGES"]),
        ("삼성SDI",        "Samsung SDI",         ["삼성SDI"]),
        ("SK이노베이션",   "SK Innovation",       ["SK이노베이션", "SK온"]),
        ("포스코퓨처엠",   "POSCO Future M",      ["포스코퓨처엠"]),
        ("엘앤에프",       "L&F",                 ["엘앤에프", "L&F"]),
    ],
    "Construction": [
        ("현대건설",  "Hyundai E&C",  ["현대건설"]),
        ("GS건설",    "GS E&C",       ["GS건설"]),
        ("삼성E&A",   "Samsung E&A",  ["삼성E&A", "삼성엔지니어링"]),
        ("삼성물산",  "Samsung C&T",  ["삼성물산"]),
    ],
    "Shipbuilding": [
        ("한화오션",      "Hanwha Ocean",    ["한화오션"]),
        ("삼성중공업",    "Samsung Heavy",   ["삼성중공업"]),
        ("HD현대중공업",  "HD Hyundai HI",   ["HD현대중공업", "현대중공업"]),
        ("한화엔진",      "Hanwha Engine",   ["한화엔진"]),
        ("HD한국조선해양", "HD Hyundai KSOE", ["HD한국조선해양", "한국조선해양", "KSOE"]),
    ],
}

# ── Auto peers (네이버 일반 검색) ─────────────────────────────────────────────
# (표시명, 네이버 검색어(한글), 제목 매칭 키워드)
AUTO_PEERS: dict[str, list[tuple[str, str, list[str]]]] = {
    "Global Peers": [
        ("Tesla",      "테슬라",       ["테슬라"]),
        ("BMW",        "BMW",          ["BMW"]),
        ("Volkswagen", "폭스바겐",     ["폭스바겐", "폴크스바겐"]),
        ("GM",         "제너럴모터스", ["제너럴모터스", "GM"]),
        ("Stellantis", "스텔란티스",   ["스텔란티스"]),
    ],
    "Japanese Peers": [
        ("Toyota",     "도요타",       ["도요타", "토요타"]),
        ("Honda",      "혼다",         ["혼다"]),
        ("Nissan",     "닛산",         ["닛산"]),
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def news_window_hours() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 24


def strip_html(text: str) -> str:
    """태그 제거 + 엔티티 완전 디코딩.
    네이버 API는 &amp;quot; 처럼 이중 인코딩해서 주는 경우가 있어,
    더 이상 변하지 않을 때까지(최대 3회) 반복 디코딩한다."""
    text = re.sub(r"<[^>]+>", "", text)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text.strip()


def title_has_company(title: str, keywords: list[str]) -> bool:
    return any(kw.lower() in title.lower() for kw in keywords)


def escape_html(text: str) -> str:
    """HTML parse_mode용 이스케이프. (반드시 & 먼저)"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def snippet_words(text: str) -> set[str]:
    """스니펫에서 2글자 이상 단어 추출.
    구두점·기호를 공백으로 치환해 조사/기호 노이즈를 줄인다."""
    text = re.sub(r"[^0-9A-Za-z가-힣]+", " ", text)
    return {w for w in text.split() if len(w) >= 2}


def is_content_duplicate(new_art: dict, accepted: list[dict]) -> bool:
    """Jaccard 유사도로 내용 중복 판별.
    ★ 임계값 이상 AND 공통 단어 DEDUP_MIN_SHARED개 이상일 때만 중복으로 본다.
      (둘 중 하나만 보면 무관한 기사도 상투어 때문에 중복 처리된다)"""
    new_words = snippet_words(new_art["snippet"])
    if len(new_words) < DEDUP_MIN_SHARED:
        return False
    for art in accepted:
        existing = snippet_words(art["snippet"])
        if len(existing) < DEDUP_MIN_SHARED:
            continue
        shared = new_words & existing
        overlap = len(shared) / len(new_words | existing)
        if overlap >= DEDUP_THRESHOLD and len(shared) >= DEDUP_MIN_SHARED:
            logger.info("SKIP (중복 %.0f%%, 공통 %d단어): %s",
                        overlap * 100, len(shared), new_art["title"][:50])
            return True
    return False


def fetch_full_title(url: str) -> Optional[str]:
    """원문 URL에서 <title> 태그로 전체 제목 추출. 실패 시 None."""
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                            timeout=5, allow_redirects=True)
        resp.raise_for_status()
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        raw = m.group(1).strip()
        raw = re.split(r"\s*[<>]\s*", raw)[0].strip()
        raw = re.sub(r"\s*[\|:：]+\s*[^|:：]{2,20}$", "", raw).strip()
        raw = re.sub(r"\s+[\-–—]\s+[^\-–—]{2,15}$", "", raw).strip()
        for _ in range(3):
            decoded = html.unescape(raw)
            if decoded == raw:
                break
            raw = decoded
        return raw or None
    except Exception as exc:
        logger.debug("fetch_full_title failed for %s: %s", url, exc)
        return None


def fetch_naver_news(korean_name: str, hours: int) -> list[dict]:
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    # ★ display 를 창 길이에 맞춰 키운다. 20건만 받으면 대형주(현대차 등)는
    #   최신 20건이 전부 최근 2~3시간치라 24h/72h 창을 제대로 못 채운다.
    params = {"query": korean_name, "display": 100 if hours >= 72 else 50,
              "sort": "date"}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Naver API error for '%s': %s", korean_name, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    articles: list[dict] = []

    for item in data.get("items", []):
        try:
            pub = parsedate_to_datetime(item["pubDate"]).astimezone(timezone.utc)
            if pub < cutoff:
                continue
            title = strip_html(item.get("title", ""))
            link  = item.get("originallink") or item.get("link", "")
            if title.endswith("...") or title.endswith("…"):
                full = fetch_full_title(link)
                if full:
                    title = full
            articles.append({
                "title":   title,
                "link":    link,
                "snippet": strip_html(item.get("description", ""))[:600],
            })
        except Exception as exc:
            logger.warning("Item parse error for '%s': %s", korean_name, exc)

    return articles


# ── Claude: 요약 / 관련성 ─────────────────────────────────────────────────────
def _claude(client: anthropic.Anthropic, prompt: str, max_tokens: int) -> str:
    """레이트리밋 시 백오프 재시도."""
    delay = 2.0
    for attempt in range(4):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except anthropic.RateLimitError:
            if attempt == 3:
                raise
            logger.warning("Anthropic 레이트리밋 — %.0f초 후 재시도", delay)
            import time
            time.sleep(delay)
            delay *= 2
    return ""


def summarise(client: anthropic.Anthropic, title: str, snippet: str) -> str:
    """핵심 포인트 2개, 요약체 bullet."""
    try:
        return _claude(client, (
            "당신은 한국 주식 담당 애널리스트 어시스턴트입니다. "
            "아래 뉴스를 핵심 포인트 정확히 2개로 요약하세요. "
            "각 줄은 '- '로 시작하고, 완전한 문장이 아닌 간결한 요약체로 쓰세요. "
            "예시: '- 2Q 영업이익 30% YoY 증가, 시장 예상치 상회' / "
            "'- capex 축소 계획 발표, 현금흐름 개선 기대'. "
            "원문에서 영어로 표기된 고유명사(기업명, 제품명, 지표 등)는 영어 그대로 유지하세요. "
            "반드시 정확히 2개의 bullet만 출력하고 다른 텍스트는 쓰지 마세요.\n\n"
            f"제목: {title}\n내용: {snippet}"
        ), 200)
    except Exception as exc:
        logger.error("Summarisation failed: %s", exc)
        return f"- {snippet[:100]}…\n- (요약 실패)"


def is_relevant(client: anthropic.Anthropic, company_kr: str, company_en: str,
                title: str, snippet: str) -> bool:
    """Claude로 관련성 필터. 회사 자체에 대한 펀더멘털 후속 기사만 통과."""
    try:
        out = _claude(client, (
            f"당신은 [{company_kr} / {company_en}]를 커버하는 한국 주식 애널리스트입니다. "
            "이 기사가 해당 기업을 '팔로업'할 가치가 있는 펀더멘털 뉴스인지 엄격하게 판단하세요.\n\n"
            "핵심 원칙: 기업명이 언급된 것만으로는 부족합니다. 기사의 '주제'가 이 기업 자체여야 하고, "
            "기업의 사업·실적·전략에 실질적 정보를 줘야 합니다.\n\n"
            "YES (통과): 실적/가이던스, 수주·계약, 수출입·생산, 신제품·기술·R&D, 설비투자(capex)·증설, "
            "M&A·지분, 업황·전방수요 변화, 규제·정책 영향, 경영전략·인사, 소송·리스크 등 "
            "기업 펀더멘털에 영향을 주는 내용.\n\n"
            "NO (제외):\n"
            "- 단순 주가 등락 기사 ('OO 3% 상승', '52주 신고가', '외국인 순매수 상위' 등)\n"
            "- 증권사 목표주가·투자의견 리포트 단순 전달 ('OO증권, 매수 유지' 류)\n"
            "- 시황·지수 기사에서 종목명만 나열된 경우\n"
            "- 스포츠 스폰서, 사회공헌, 봉사/행사, ESG 홍보성\n"
            "- 동명이인·동명 기업 등 해당 기업과 무관\n"
            "- 내용 없는 단순 공시 알림\n\n"
            "반드시 YES 또는 NO 한 단어만 출력하세요.\n\n"
            f"제목: {title}\n내용: {snippet}"
        ), 5)
        return out.upper().startswith("YES")
    except Exception as exc:
        logger.error("Relevance check failed: %s", exc)
        return True  # 판단 실패 시 포함


def is_relevant_peer(client: anthropic.Anthropic, peer: str,
                     title: str, snippet: str) -> bool:
    """글로벌 peer 관련성 필터. 한국 OEM read-through 관점."""
    try:
        out = _claude(client, (
            f"당신은 한국 자동차 섹터(현대차·기아 등)를 커버하는 애널리스트입니다. "
            f"글로벌 peer [{peer}] 관련 국내 매체 기사입니다. "
            "한국 OEM 대비 경쟁구도·산업 read-through 관점에서 "
            "'반드시 팔로업할 만큼 중요한 펀더멘털/전략 뉴스'인지 엄격히 판단하세요.\n\n"
            "YES: 실적·가이던스, 글로벌 생산·판매 동향, EV·신차·플랫폼 전략, 가격정책·인센티브, "
            "공급망·관세·통상·정책, 대규모 투자/감산/구조조정/공장, 대형 리콜·소송·파업 등.\n"
            "NO: 단순 주가·시총, 증권사 목표주가, 루머·가십, 개별 딜러/지역 행사, "
            "단순 모델 리뷰·시승기, 중복성 시황, 나열성 기사.\n\n"
            "반드시 YES 또는 NO 한 단어만.\n\n"
            f"제목: {title}\n내용: {snippet}"
        ), 5)
        return out.upper().startswith("YES")
    except Exception as exc:
        logger.error("Peer relevance check failed: %s", exc)
        return True


# ── Block / message builders ─────────────────────────────────────────────────
TITLE_MARK = "■"


def build_article_block(art: dict, client: anthropic.Anthropic) -> str:
    summary = summarise(client, art["title"], art["snippet"])
    return (
        f"{TITLE_MARK} <b>{escape_html(art['title'])}</b>\n"
        f"{escape_html(art['link'])}\n"
        f"{escape_html(summary)}\n"
    )


def split_into_messages(header: str, blocks: list[str]) -> list[str]:
    """기사 블록들을 텔레그램 크기에 맞게 분할.
    ★ 블록 하나가 한도를 넘는 경우도 강제로 잘라 전송 실패를 막는다."""
    messages: list[str] = []
    current = header + "\n\n"

    for block in blocks:
        # 단일 블록이 한도를 넘으면 잘라낸다 (제목이 비정상적으로 긴 경우 등)
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


async def _send(bot: Bot, text: str, label: str) -> None:
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        await asyncio.sleep(1)
    except TelegramError as exc:
        logger.error("Send failed (%s): %s", label, exc)


# ── Collection ───────────────────────────────────────────────────────────────
def collect(entries, hours, client, seen_titles, accepted_articles, peer: bool):
    """공통 수집 로직. (priority, normal) 반환 + 드롭 사유 카운트 로깅."""
    priority: list[dict] = []
    normal: list[dict] = []
    stats = {"fetched": 0, "dup_title": 0, "irrelevant": 0, "dup_body": 0, "kept": 0}

    for name_a, name_b, keywords in entries:
        # 커버리지: (한글명, 영문명, kw) / peer: (표시명, 검색어, kw)
        query   = name_b if peer else name_a
        label   = name_a if peer else name_b
        company_count = 0

        for art in fetch_naver_news(query, hours):
            stats["fetched"] += 1
            if MAX_PER_COMPANY is not None and company_count >= MAX_PER_COMPANY:
                break
            key = art["title"][:60].lower()
            if key in seen_titles:
                stats["dup_title"] += 1
                continue
            ok = (is_relevant_peer(client, label, art["title"], art["snippet"])
                  if peer else
                  is_relevant(client, name_a, name_b, art["title"], art["snippet"]))
            if not ok:
                stats["irrelevant"] += 1
                continue
            if is_content_duplicate(art, accepted_articles):
                stats["dup_body"] += 1
                seen_titles.add(key)
                continue
            seen_titles.add(key)
            accepted_articles.append(art)
            company_count += 1
            stats["kept"] += 1
            art["company"] = label
            (priority if title_has_company(art["title"], keywords) else normal).append(art)

    logger.info("수집: 조회 %d → 제목중복 %d / 무관 %d / 내용중복 %d → 채택 %d",
                stats["fetched"], stats["dup_title"], stats["irrelevant"],
                stats["dup_body"], stats["kept"])
    return priority + normal


async def build_and_send(bot: Bot, header_text: str, entries, hours, client,
                         seen_titles, accepted_articles, peer: bool = False) -> bool:
    articles = await asyncio.to_thread(
        collect, entries, hours, client, seen_titles, accepted_articles, peer
    )
    if not articles:
        logger.info("%s: 기사 없음", header_text)
        return False

    blocks = await asyncio.to_thread(
        lambda: [build_article_block(a, client) for a in articles]
    )
    messages = split_into_messages(f"<b>{escape_html(header_text)}</b>", blocks)

    for i, msg in enumerate(messages):
        if i > 0:
            msg = f"<b>{escape_html(header_text)} (계속)</b>\n\n" + msg.lstrip()
        await _send(bot, msg, f"{header_text} #{i + 1}")

    return True


# ── Core job ─────────────────────────────────────────────────────────────────
async def send_news_brief(bot: Bot, client: anthropic.Anthropic) -> None:
    now   = datetime.now(KST)
    hours = news_window_hours()
    label = "72h (Mon)" if hours == 72 else "24h"

    logger.info("Running brief — %s | window: %s",
                now.strftime("%Y-%m-%d %H:%M KST"), label)

    header = (
        f"<b>Equity Research News Brief</b>\n"
        f"<i>{now.strftime('%Y.%m.%d %H:%M KST')} · {label}</i>\n"
        f"{'─' * 30}"
    )
    try:
        await bot.send_message(chat_id=CHAT_ID, text=header, parse_mode="HTML")
    except TelegramError as exc:
        logger.error("Header send failed: %s", exc)
        return

    found_any = False
    seen_titles: set[str] = set()
    accepted_articles: list[dict] = []

    for sector, companies in COVERAGE.items():
        if await build_and_send(bot, sector, companies, hours, client,
                                seen_titles, accepted_articles):
            found_any = True

    for group, peers in AUTO_PEERS.items():
        if await build_and_send(bot, f"Auto Peers — {group}", peers, hours, client,
                                seen_titles, accepted_articles, peer=True):
            found_any = True

    if not found_any:
        await _send(bot, "<i>해당 윈도우 내 새 기사 없음</i>", "empty")

    logger.info("Brief sent. 총 채택 %d건", len(accepted_articles))


# ── Entry point ──────────────────────────────────────────────────────────────
async def main() -> None:
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
                "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing env vars: {', '.join(missing)}  "
            "— .env 파일을 확인하세요 (python check.py 로 진단 가능)"
        )

    run_once = "--now" in sys.argv
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    async with Bot(token=BOT_TOKEN) as bot:
        if run_once:
            await send_news_brief(bot, client)
            return

        scheduler = AsyncIOScheduler(timezone=KST)
        scheduler.add_job(
            send_news_brief,
            CronTrigger(hour="6,13,18", minute=0, timezone=KST),
            args=[bot, client],
            id="news_brief",
            max_instances=1,
            misfire_grace_time=120,
        )
        scheduler.start()
        logger.info("Scheduler ready — 06:00 / 13:00 / 18:00 KST")

        await send_news_brief(bot, client)

        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
