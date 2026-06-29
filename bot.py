#!/usr/bin/env python3
"""
Equity Research News Bot — Naver News edition
Sends sector-grouped Korean news briefs every 4 hours from 06:00 HKT.
Monday window: 72h | Tue–Sun window: 24h
"""

import os
import re
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

TELEGRAM_MAX_CHARS = 3800  # 텔레그램 한도 4096에서 여유 확보

# ── Coverage universe ────────────────────────────────────────────────────────
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
        ("LG에너지솔루션", "LG Energy Solution", ["LG에너지솔루션", "LGES"]),
        ("삼성SDI",        "Samsung SDI",         ["삼성SDI"]),
        ("SK이노베이션",   "SK Innovation",       ["SK이노베이션", "SK온"]),
        ("포스코퓨처엠",   "POSCO Future M",      ["포스코퓨처엠"]),
        ("L&F",            "L&F",                 ["L&F"]),
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
        ("HD현대KSOE",    "HD Hyundai KSOE", ["HD현대KSOE", "KSOE"]),
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def news_window_hours() -> int:
    return 72 if datetime.now(KST).weekday() == 0 else 24


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def title_has_company(title: str, keywords: list[str]) -> bool:
    return any(kw.lower() in title.lower() for kw in keywords)


def escape_html(text: str) -> str:
    """HTML parse_mode용 이스케이프."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def snippet_words(text: str) -> set[str]:
    """스니펫에서 2글자 이상 단어 추출 (조사·숫자 제외)."""
    return {w for w in re.split(r"\s+", text) if len(w) >= 2}


def is_content_duplicate(new_art: dict, accepted: list[dict], threshold: float = 0.05) -> bool:
    """Jaccard 유사도로 내용 중복 판별. threshold 이상이면 중복으로 간주."""
    new_words = snippet_words(new_art["snippet"])
    if not new_words:
        return False
    for art in accepted:
        existing = snippet_words(art["snippet"])
        if not existing:
            continue
        overlap = len(new_words & existing) / len(new_words | existing)
        if overlap >= threshold:
            logger.info("SKIP (duplicate content, %.0f%%): %s", overlap * 100, new_art["title"][:60])
            return True
    return False


def fetch_full_title(url: str) -> Optional[str]:
    """원문 URL에서 <title> 태그로 전체 제목 추출. 실패 시 None 반환."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
            allow_redirects=True,
        )
        resp.raise_for_status()
        # <title>...</title> 추출
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        raw = m.group(1).strip()
        # 1) 사이트 네비게이션 경로 제거 (예: "제목 < 기업PR < 퍼블릭 핫뉴스 < 기사본문")
        #    첫 '<' 이후 전체를 잘라냄
        raw = re.split(r"\s*[<>]\s*", raw)[0].strip()
        # 2) 언론사명 suffix 제거 (예: " | 한국경제", " - 연합뉴스", " :: 머니투데이")
        #    중간점(·∙)은 한국어 제목에 정상적으로 쓰이므로 구분자에서 제외
        raw = re.sub(r"\s*[\|:：]+\s*[^|:：]{2,20}$", "", raw).strip()
        raw = re.sub(r"\s+[\-–—]\s+[^\-–—]{2,15}$", "", raw).strip()
        return html.unescape(raw) or None
    except Exception as exc:
        logger.debug("fetch_full_title failed for %s: %s", url, exc)
        return None


def fetch_naver_news(korean_name: str, hours: int) -> list[dict]:
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": korean_name, "display": 20, "sort": "date"}

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
            # 제목이 잘린 경우 원문에서 전체 제목 가져오기
            if title.endswith("...") or title.endswith("…"):
                full = fetch_full_title(link)
                if full:
                    logger.info("Title restored: %s → %s", title[:40], full[:60])
                    title = full
            articles.append({
                "title":   title,
                "link":    link,
                "snippet": strip_html(item.get("description", ""))[:600],
            })
        except Exception as exc:
            logger.warning("Item parse error for '%s': %s", korean_name, exc)

    return articles


def summarise(client: anthropic.Anthropic, title: str, snippet: str) -> str:
    """핵심 포인트 2개, 요약체 bullet."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "당신은 한국 주식 담당 애널리스트 어시스턴트입니다. "
                    "아래 뉴스를 핵심 포인트 정확히 2개로 요약하세요. "
                    "각 줄은 '- '로 시작하고, 완전한 문장이 아닌 간결한 요약체로 쓰세요. "
                    "예시: '- 2Q 영업이익 30% YoY 증가, 시장 예상치 상회' / '- capex 축소 계획 발표, 현금흐름 개선 기대'. "
                    "원문에서 영어로 표기된 고유명사(기업명, 제품명, 지표 등)는 영어 그대로 유지하세요. "
                    "반드시 정확히 2개의 bullet만 출력하고 다른 텍스트는 쓰지 마세요.\n\n"
                    f"제목: {title}\n내용: {snippet}"
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        logger.error("Summarisation failed: %s", exc)
        return f"- {snippet[:100]}…\n- (요약 실패)"


def is_relevant(client: anthropic.Anthropic, company_kr: str, company_en: str, title: str, snippet: str) -> bool:
    """Claude로 관련성 필터. 회사 자체에 대한 펀더멘털 후속 기사만 통과."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{
                "role": "user",
                "content": (
                    f"당신은 [{company_kr} / {company_en}]를 커버하는 한국 주식 애널리스트입니다. "
                    "이 기사가 해당 기업을 '팔로업'할 가치가 있는 펀더멘털 뉴스인지 엄격하게 판단하세요.\n\n"
                    "핵심 원칙: 기업명이 언급된 것만으로는 부족합니다. 기사의 '주제'가 이 기업 자체여야 하고, "
                    "기업의 사업·실적·전략에 실질적 정보를 줘야 합니다.\n\n"
                    "YES (통과): 실적/가이던스, 수주·계약, 수출입·생산, 신제품·기술·R&D, 설비투자(capex)·증설, "
                    "M&A·지분, 업황·전방수요 변화, 규제·정책 영향, 경영전략·인사, 소송·리스크 등 "
                    "기업 펀더멘털에 영향을 주는 내용.\n\n"
                    "NO (제외):\n"
                    "- 단순 주가 등락 기사 ('OO 3% 상승', '52주 신고가', '외국인 순매수 상위' 등 가격/수급만 다룸)\n"
                    "- 증권사 목표주가·투자의견 리포트 단순 전달 ('OO증권, 매수 유지' 류)\n"
                    "- 시황·지수 기사에서 종목명만 나열된 경우 (예: '코스피 하락… 삼성SDI·LG엔솔 약세')\n"
                    "- 스포츠 스폰서, 사회공헌, 봉사/행사, ESG 홍보성\n"
                    "- 동명이인·동명 기업 등 해당 기업과 무관\n"
                    "- 내용 없는 단순 공시 알림\n\n"
                    "반드시 YES 또는 NO 한 단어만 출력하세요.\n\n"
                    f"제목: {title}\n내용: {snippet}"
                ),
            }],
        )
        return resp.content[0].text.strip().upper().startswith("YES")
    except Exception as exc:
        logger.error("Relevance check failed: %s", exc)
        return True  # 판단 실패 시 포함


def build_article_block(art: dict, client: anthropic.Anthropic) -> str:
    """단일 기사를 포맷팅된 문자열로 반환."""
    summary = summarise(client, art["title"], art["snippet"])
    return f"<b>{escape_html(art['title'])}</b>\n{art['link']}\n{summary}\n"


def split_into_messages(header: str, blocks: list[str]) -> list[str]:
    """
    기사 블록들을 텔레그램 메시지 크기에 맞게 분할.
    기사 중간에서 자르지 않고 기사 단위로 분할.
    """
    messages: list[str] = []
    current = header + "\n\n"

    for block in blocks:
        # 이 블록 추가하면 한도 초과하는지 확인
        if len(current) + len(block) > TELEGRAM_MAX_CHARS and current != header + "\n\n":
            messages.append(current.rstrip())
            current = block
        else:
            current += block + "\n"

    if current.strip():
        messages.append(current.rstrip())

    return messages


# ── Message builder ──────────────────────────────────────────────────────────
async def build_and_send_sector(
    bot: Bot,
    sector: str,
    companies: list[tuple[str, str, list[str]]],
    hours: int,
    client: anthropic.Anthropic,
    seen_titles: set[str],
    accepted_articles: list[dict],
) -> bool:
    """섹터 기사 수집 → 중복 제거 → 포맷 → 분할 전송. 기사 있으면 True 반환."""
    priority: list[dict] = []
    normal: list[dict] = []

    for korean_name, eng_name, keywords in companies:
        for art in fetch_naver_news(korean_name, hours):
            key = art["title"][:60].lower()
            if key in seen_titles:
                continue
            # 관련성 필터 — 무관한 기사 제거
            if not is_relevant(client, korean_name, eng_name, art["title"], art["snippet"]):
                logger.info("SKIP (irrelevant): %s", art["title"][:60])
                continue
            # 내용 중복 필터 — 사이클 내 이미 보낸 기사와 내용이 유사하면 제거
            if is_content_duplicate(art, accepted_articles):
                seen_titles.add(key)
                continue
            seen_titles.add(key)
            accepted_articles.append(art)
            art["company"] = eng_name
            if title_has_company(art["title"], keywords):
                priority.append(art)
            else:
                normal.append(art)

    articles = priority + normal
    if not articles:
        return False

    # 기사별 블록 생성
    blocks = [build_article_block(art, client) for art in articles]

    # 섹터 헤더 (첫 메시지에만)
    header = f"<b>{sector}</b>"
    messages = split_into_messages(header, blocks)

    for i, msg in enumerate(messages):
        if i > 0:
            msg = f"<b>{sector} (계속)</b>\n\n" + msg.lstrip()
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await asyncio.sleep(1)
        except TelegramError as exc:
            logger.error("Send failed for '%s' msg %d: %s", sector, i + 1, exc)

    return True


# ── Core job ─────────────────────────────────────────────────────────────────
async def send_news_brief(bot: Bot, client: anthropic.Anthropic) -> None:
    now   = datetime.now(KST)
    hours = news_window_hours()
    label = "72h (Mon)" if hours == 72 else "24h"

    logger.info("Running brief — %s | window: %s", now.strftime("%Y-%m-%d %H:%M KST"), label)

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
    seen_titles: set[str] = set()       # 제목 기반 중복 제거
    accepted_articles: list[dict] = []  # 내용 기반 중복 제거용 누적 목록
    for sector, companies in COVERAGE.items():
        had_news = await build_and_send_sector(bot, sector, companies, hours, client, seen_titles, accepted_articles)
        if had_news:
            found_any = True

    if not found_any:
        await bot.send_message(
            chat_id=CHAT_ID,
            text="<i>해당 윈도우 내 새 기사 없음</i>",
            parse_mode="HTML",
        )

    logger.info("Brief sent.")


# ── Entry point ──────────────────────────────────────────────────────────────
async def main() -> None:
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
                "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")

    bot    = Bot(token=BOT_TOKEN)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
    asyncio.run(main())
