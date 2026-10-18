"""Monitor Regiondo ticket titles and add a matching product to the cart."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


TIME_ZONE = "Europe/Berlin"
DEFAULT_PRODUCT_URL = "https://audidefuehrungen2.regiondo.de/kategorien"
CART_URL = "https://audidefuehrungen2.regiondo.de/checkout/cart/index"
TICKET_LIMIT = 2
LOG = logging.getLogger("ticketfinder")
PRICE_RE = re.compile(r"(?:€\s*|\bEUR\s*)([\d.,]+)|([\d.,]+)\s*(?:€|\bEUR\b)", re.I)


class ConfigurationError(ValueError):
    """Raised when the CLI or environment configuration is invalid."""


class PageStateError(RuntimeError):
    """Raised when the page state is ambiguous and an action is unsafe."""


class TicketfinderArgumentParser(argparse.ArgumentParser):
    """Translate common argparse validation errors for the Chinese CLI."""

    def error(self, message: str) -> None:
        if message.startswith("the following arguments are required:"):
            message = "缺少必填参数：" + message.split(":", 1)[1].strip()
        elif "invalid int value" in message:
            message = "参数格式无效；-i 必须是整数秒数"
        super().error(message)


@dataclass(frozen=True)
class Settings:
    keywords: tuple[str, ...]
    interval: int
    add_to_cart: bool
    product_url: str
    headless: bool
    username: Optional[str]
    password: Optional[str]
    smtp_host: str
    smtp_port: int
    smtp_username: Optional[str]
    smtp_password: Optional[str]
    email_from: str
    email_to: str


@dataclass(frozen=True)
class TicketTarget:
    """The first matching product discovered in pagination order."""

    title: str
    product_url: str
    listing_page_url: str
    sold_out_on_listing: bool = False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line options. The command-line interval is in seconds."""
    parser = TicketfinderArgumentParser(
        prog="ticketfinder",
        description="监控票务页面，并按需加购符合条件的票务。",
    )
    parser.add_argument(
        "-d", dest="start_date", help="程序开始日期，格式 DD.MM.YYYY；省略时需同时省略 -t"
    )
    parser.add_argument(
        "-t", dest="start_time", help="程序开始时间，格式 HH:MM（柏林时间）；省略时需同时省略 -d"
    )
    parser.add_argument(
        "-i", dest="interval", type=int, default=60,
        help="未找到可购票务时的刷新间隔，单位秒（默认：60）",
    )
    parser.add_argument(
        "-k", dest="keyword", required=True,
        help='必填；多个关键词用逗号分隔，且都必须出现在同一标题中（例如："FC MAGDEBURG, Ingolstadt"）',
    )
    parser.add_argument(
        "-c", dest="add_to_cart", action="store_true",
        help="找到可购票务后加入购物车；省略时只发送邮件通知",
    )
    args = parser.parse_args(argv)

    if bool(args.start_date) != bool(args.start_time):
        parser.error("-d 和 -t 必须同时提供，或同时省略")
    if args.interval <= 0:
        parser.error("-i 必须是正整数秒数")
    args.keywords = tuple(term.strip() for term in args.keyword.split(",") if term.strip())
    if not args.keywords:
        parser.error("-k 至少需要一个非空关键词")

    if args.start_date:
        try:
            args.scheduled_start = datetime.combine(
                datetime.strptime(args.start_date, "%d.%m.%Y").date(),
                datetime.strptime(args.start_time, "%H:%M").time(),
                tzinfo=ZoneInfo(TIME_ZONE),
            )
        except (ValueError, ZoneInfoNotFoundError) as exc:
            parser.error(f"-d 或 -t 格式无效：{exc}")
    else:
        args.scheduled_start = None
    return args


def load_settings(args: argparse.Namespace) -> Settings:
    """Load secrets and site settings from the local .env file."""
    load_dotenv()
    product_url = os.getenv("PRODUCT_URL", DEFAULT_PRODUCT_URL).strip()
    if not product_url.startswith("https://"):
        raise ConfigurationError("PRODUCT_URL 必须是 https:// 开头的地址")

    username = os.getenv("SITE_USERNAME", "").strip() or None
    password = os.getenv("SITE_PASSWORD", "") or None
    if bool(username) != bool(password):
        raise ConfigurationError("SITE_USERNAME 和 SITE_PASSWORD 必须同时填写，或同时留空")

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    email_from = os.getenv("EMAIL_FROM", "").strip()
    email_to = os.getenv("EMAIL_TO", "").strip()
    if not smtp_host or not email_from or not email_to:
        raise ConfigurationError("请在 .env 中填写 SMTP_HOST、EMAIL_FROM 和 EMAIL_TO")
    for label, address in (("EMAIL_FROM", email_from), ("EMAIL_TO", email_to)):
        if parseaddr(address)[1] != address or "@" not in address:
            raise ConfigurationError(f"{label} 不是有效的电子邮件地址")
    try:
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise ConfigurationError("SMTP_PORT 必须是整数") from exc
    if not 1 <= smtp_port <= 65535:
        raise ConfigurationError("SMTP_PORT 必须在 1 到 65535 之间")
    smtp_username = os.getenv("SMTP_USERNAME", "").strip() or None
    smtp_password = os.getenv("SMTP_PASSWORD", "") or None
    if bool(smtp_username) != bool(smtp_password):
        raise ConfigurationError("SMTP_USERNAME 和 SMTP_PASSWORD 必须同时填写，或同时留空")

    return Settings(
        keywords=args.keywords,
        interval=args.interval,
        add_to_cart=args.add_to_cart,
        product_url=product_url,
        headless=os.getenv("HEADLESS", "false").strip().lower() not in {"0", "false", "no"},
        username=username,
        password=password,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        email_from=email_from,
        email_to=email_to,
    )


def configure_logging() -> None:
    """Write Chinese status logs with Berlin-local timestamps."""

    class BerlinFormatter(logging.Formatter):
        def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:
            timestamp = datetime.fromtimestamp(record.created, ZoneInfo(TIME_ZONE))
            return timestamp.strftime(datefmt or "%d.%m.%Y %H:%M:%S")

    formatter = BerlinFormatter("%(asctime)s %(message)s", "%d.%m.%Y %H:%M:%S")
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler = logging.FileHandler("ticketfinder.log", encoding="utf-8")
    stream_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[stream_handler, file_handler],
    )


async def wait_for_start(start_at: Optional[datetime]) -> None:
    """Wait interruptibly until the requested Berlin local date and time."""
    if start_at is None:
        return
    now = datetime.now(ZoneInfo(TIME_ZONE))
    if start_at <= now:
        LOG.info("设定的启动时间已过，立即开始")
        return
    LOG.info("等待至 %s（柏林时间）开始", start_at.strftime("%d.%m.%Y %H:%M"))
    while True:
        remaining = (start_at - datetime.now(ZoneInfo(TIME_ZONE))).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 30))


def _parse_price(text: str) -> Optional[float]:
    """Extract a EUR amount from a visible option label."""
    match = PRICE_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    value = (match.group(1) or match.group(2)).replace(".", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def _ticket_option_price(option_text: str, pricing_text: str) -> Optional[float]:
    """Get an option's price, pairing category/block labels with page price details.

    Regiondo sometimes renders prices in the product description while the
    variation dropdown contains only labels such as "Block 237 - Kategorie 3".
    """
    price = _parse_price(option_text)
    if price is not None:
        return price

    category = re.search(r"\bKategorie\s*(\d+)\b", option_text, re.I)
    block = re.search(r"\bBlock\s*([A-Za-z0-9]+)\b", option_text, re.I)
    if not category or not block:
        return None

    wanted_category = category.group(1)
    wanted_block = block.group(1).casefold()
    price_entries = re.finditer(
        r"\bKategorie\s*(\d+)\s*[-–]\s*Block\s*([A-Za-z0-9]+)\s*:\s*"
        r"([0-9][0-9.,]*)\s*(?:€|EUR)",
        pricing_text,
        re.I,
    )
    for entry in price_entries:
        if entry.group(1) == wanted_category and entry.group(2).casefold() == wanted_block:
            return _parse_price(f"{entry.group(3)} €")
    return None


async def handle_cookie_consent(page: Page) -> None:
    """Dismiss Regiondo's consent banner by rejecting optional cookies only."""
    customize_button = page.get_by_role(
        "button", name=re.compile(r"Ablehnen oder Einstellungen", re.I)
    )
    deny_button = page.get_by_role("button", name=re.compile(r"^Alle ablehnen$", re.I))

    # Usercentrics may inject its shadow-DOM banner after the main document's
    # DOMContentLoaded event. Wait briefly for its reject controls when present.
    consent_root = page.locator("#usercentrics-root")
    if await consent_root.count():
        for _ in range(10):
            if (
                (await deny_button.count() and await deny_button.first.is_visible())
                or (await customize_button.count() and await customize_button.first.is_visible())
            ):
                break
            await page.wait_for_timeout(300)

    if await deny_button.count() and await deny_button.first.is_visible():
        await deny_button.first.click()
        LOG.info("已拒绝网站的非必要 Cookie")
        return
    if not await customize_button.count() or not await customize_button.first.is_visible():
        return

    # The initial Usercentrics screen offers a settings/rejection action. Open
    # it, then choose its explicit reject-all option; never accept all cookies.
    await customize_button.first.click()
    deny_button = page.get_by_role("button", name=re.compile(r"^Alle ablehnen$", re.I))
    try:
        await deny_button.first.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeoutError as exc:
        raise PageStateError(
            "已打开 Cookie 设置，但未找到明确的拒绝全部选项"
        ) from exc
    await deny_button.first.click()
    LOG.info("已在 Cookie 设置中拒绝非必要 Cookie")


async def handle_login_if_present(page: Page, settings: Settings) -> None:
    """Complete an ordinary visible login form if the site presents one."""
    password_fields = page.locator('input[type="password"]:visible')
    if await password_fields.count() == 0:
        return
    if not settings.username or not settings.password:
        raise PageStateError("页面要求登录；请在 .env 中配置 SITE_USERNAME 和 SITE_PASSWORD")

    password = password_fields.first
    form = password.locator("xpath=ancestor::form[1]")
    user_field = form.locator(
        'input[type="email"], input[autocomplete="username"], input[name*="user" i], '
        'input[name*="email" i], input[type="text"]'
    ).first
    if await user_field.count() == 0:
        raise PageStateError("发现登录表单，但无法识别账号输入框")
    await user_field.fill(settings.username)
    await password.fill(settings.password)
    submit = form.locator('button[type="submit"], input[type="submit"]').first
    if await submit.count() == 0:
        raise PageStateError("发现登录表单，但无法识别登录按钮")
    await submit.click()
    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    if await page.locator('input[type="password"]:visible').count():
        raise PageStateError("提交后登录表单仍然显示；请检查账号密码或页面提示")
    LOG.info("网站登录成功")


def title_matches(title: str, keywords: tuple[str, ...]) -> bool:
    """Return true only when every term occurs in this one ticket title."""
    normalized_title = " ".join(title.split()).casefold()
    return bool(normalized_title) and all(term.casefold() in normalized_title for term in keywords)


async def listing_titles_and_links(page: Page) -> list[tuple[str, str, bool]]:
    """Extract ticket titles, product URLs, and card-local sold-out state."""
    rows = await page.locator("body").evaluate(
        """body => {
          const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
          const rows = [];
          const cardIsSoldOut = heading => {
            let node = heading.parentElement;
            for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
              const labels = [...node.querySelectorAll('[aria-label],[title],img[alt]')]
                .map(el => el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '')
                .join(' ');
              const text = clean(`${node.innerText || ''} ${labels}`);
              const headingCount = node.querySelectorAll('h1,h2,h3,h4').length;
              if (headingCount <= 1 && text.length < 1200 && /\\bausverkauft\\b/i.test(text)) return true;
            }
            return false;
          };
          const add = (title, href, soldOut=false) => {
            title = clean(title);
            if (!title || !href || href.startsWith('javascript:') || href.endsWith('#')) return;
            rows.push({title, href, soldOut});
          };

          // Product titles are commonly headings linked directly to their detail pages.
          for (const heading of body.querySelectorAll('h1,h2,h3,h4')) {
            let link = heading.closest('a[href]') || heading.querySelector('a[href]');
            if (!link) {
              let parent = heading.parentElement;
              for (let depth = 0; parent && depth < 4 && !link; depth++, parent = parent.parentElement) {
                const candidates = [...parent.querySelectorAll('a[href]')];
                link = candidates.find(a => !/kategorien|checkout|cart/i.test(a.getAttribute('href') || ''));
              }
            }
            if (link) add(heading.innerText, link.href, cardIsSoldOut(heading));
          }

          // Also support cards whose product title is stored on a linked image or card.
          for (const anchor of body.querySelectorAll('a[href]')) {
            const titleNode = anchor.querySelector('h1,h2,h3,h4,[class*=product-name],[class*=product-title]');
            const title = titleNode?.innerText || anchor.getAttribute('title') || anchor.getAttribute('aria-label');
            if (title) add(title, anchor.href);
          }
          return rows;
        }"""
    )
    unique: dict[tuple[str, str], bool] = {}
    for row in rows:
        href = urljoin(page.url, row["href"])
        parts = urlsplit(href)
        if parts.scheme == "https" and parts.hostname == urlsplit(page.url).hostname:
            key = (" ".join(row["title"].split()), href)
            unique[key] = unique.get(key, False) or bool(row["soldOut"])
    return [(title, href, sold_out) for (title, href), sold_out in unique.items()]


async def discover_first_matching_product(
    page: Page, listing_url: str, keywords: tuple[str, ...], settings: Settings
) -> Optional[TicketTarget]:
    """Find the first matching product in page/card order and lock its URL.

    If PRODUCT_URL is already a product page, its primary heading is checked.
    For category pages, all numbered Regiondo pagination links are followed until
    no new page number is discovered. There is intentionally no fixed page cap.
    """
    pending = [listing_url]
    visited: set[tuple[str, str, str]] = set()
    parsed_start = urlsplit(listing_url)
    category_fallback = f"{parsed_start.scheme}://{parsed_start.netloc}/kategorien"
    if parsed_start.path.rstrip("/") != "/kategorien":
        pending.append(category_fallback)
    while pending:
        current_url = pending.pop(0)
        current_parts = urlsplit(current_url)
        if current_parts.hostname != parsed_start.hostname or current_parts.path.rstrip("/") != parsed_start.path.rstrip("/") and current_parts.path.rstrip("/") != "/kategorien":
            continue
        page_number = parse_qs(current_parts.query).get("p", ["1"])[0]
        page_key = (current_parts.hostname or "", current_parts.path.rstrip("/"), page_number)
        if page_key in visited:
            continue
        visited.add(page_key)
        if current_parts.path.rstrip("/") == "/kategorien":
            LOG.info("扫描购票分类第 %s 页", page_number)
        else:
            LOG.info("检查配置的商品详情页")
        await page.goto(current_url, wait_until="domcontentloaded", timeout=30_000)
        await handle_cookie_consent(page)
        await handle_login_if_present(page, settings)
        try:
            primary_title = " ".join((await page.locator("h1").first.inner_text(timeout=1_500)).split())
        except PlaywrightError:
            primary_title = ""
        if current_parts.path.rstrip("/") != "/kategorien" and title_matches(primary_title, keywords):
            LOG.info("找到包含关键词 %s 的票务：%s", "、".join(keywords), primary_title)
            return TicketTarget(primary_title, page.url, page.url)

        rows = await listing_titles_and_links(page)
        match = next(
            ((title, href, sold_out) for title, href, sold_out in rows if title_matches(title, keywords)),
            None,
        )
        if match:
            title, product_url, sold_out = match
            if urlsplit(product_url).path != urlsplit(page.url).path:
                LOG.info("找到包含关键词 %s 的票务：%s", "、".join(keywords), title)
                return TicketTarget(" ".join(title.split()), product_url, page.url, sold_out)

        # Collect Regiondo category page links such as ?p=2 and ?p=3.
        hrefs = await page.locator("a[href]").evaluate_all(
            "els => els.map(a => a.href).filter(h => /[?&]p=\\d+/i.test(h))"
        )
        for href in hrefs:
            absolute = urljoin(page.url, href)
            query = parse_qs(urlsplit(absolute).query)
            page_number = query.get("p", [""])[0]
            parts = urlsplit(absolute)
            key = (parts.hostname or "", parts.path.rstrip("/"), page_number)
            if (
                parts.hostname == parsed_start.hostname
                and parts.path.rstrip("/") == current_parts.path.rstrip("/")
                and page_number.isdigit()
                and key not in visited
                and absolute not in pending
            ):
                pending.append(absolute)
        pending.sort(key=lambda url: int(parse_qs(urlsplit(url).query).get("p", ["1"])[0]))

    return None


async def choose_lowest_price(page: Page) -> str:
    """Select the cheapest visible priced radio/select option when available."""
    choices: list[tuple[float, str, Locator, str]] = []
    pricing_text = " ".join((await page.locator("body").inner_text()).split())

    radios = page.locator('input[type="radio"]:visible')
    for index in range(await radios.count()):
        radio = radios.nth(index)
        try:
            radio_id = await radio.get_attribute("id")
            if radio_id:
                label = page.locator(f'label[for="{radio_id}"]')
                label_text = await label.inner_text(timeout=700)
            else:
                label_text = await radio.locator("xpath=ancestor::label[1]").inner_text(timeout=700)
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
        price = _parse_price(label_text)
        if price is not None and await radio.is_enabled():
            choices.append((price, label_text.strip(), radio, "radio"))

    selects = page.locator("select:visible")
    for select_index in range(await selects.count()):
        select = selects.nth(select_index)
        options = await select.locator("option").evaluate_all(
            "els => els.map(e => ({value:e.value, text:(e.innerText || e.textContent || '').trim(), "
            "disabled:e.disabled}))"
        )
        for option in options:
            price = _ticket_option_price(option["text"], pricing_text)
            if price is not None and not option["disabled"] and option["value"]:
                choices.append((price, option["text"], select, f"select:{option['value']}"))

    # Some booking widgets expose ticket categories as accessible radio-like
    # elements instead of native <input type="radio"> controls.
    role_radios = page.locator('[role="radio"]:visible')
    for index in range(await role_radios.count()):
        control = role_radios.nth(index)
        try:
            label_text = await control.inner_text(timeout=700)
        except PlaywrightError:
            continue
        price = _parse_price(label_text)
        if price is not None and await control.is_enabled():
            choices.append((price, label_text.strip(), control, "role-radio"))

    if not choices:
        raise PageStateError("未找到带有可见价格且可选择的票档")

    price, label, control, kind = min(choices, key=lambda choice: choice[0])
    if kind == "radio":
        await control.check()
    elif kind == "role-radio":
        await control.click()
    else:
        await control.select_option(value=kind.split(":", 1)[1])
    return f"{label} (€{price:.2f})"


async def set_quantity_to_two(page: Page) -> None:
    """Select two adult tickets and zero discounted tickets in Regiondo's form."""
    adult_pattern = re.compile(r"\b(adult|erwachsene?r?)\b", re.I)
    reduced_pattern = re.compile(
        r"(ermäßigt|ermaessigt|reduced|discount|child|children|kinder?)", re.I
    )

    # Selecting the ticket variation can reveal the attendee quantity dropdowns
    # asynchronously, so wait briefly for those controls to be rendered.
    adult_selects: list[Locator] = []
    reduced_selects: list[Locator] = []
    generic_quantity_selects: list[Locator] = []
    for attempt in range(12):
        selects = page.locator("select:visible")
        for index in range(await selects.count()):
            select = selects.nth(index)
            descriptor = " ".join(
                filter(
                    None,
                    [
                        await select.get_attribute("name"),
                        await select.get_attribute("id"),
                        await select.get_attribute("aria-label"),
                        await select.get_attribute("aria-labelledby"),
                    ],
                )
            ).casefold()
            nearby_text = await select.evaluate(
                """el => {
                  for (let node = el.parentElement, depth = 0; node && depth < 6;
                       node = node.parentElement, depth++) {
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (node.querySelectorAll('select').length === 1 && text.length < 300) return text;
                  }
                  return el.parentElement?.innerText || '';
                }"""
            )
            options = await select.locator("option").evaluate_all(
                "els => els.map(e => ({value:e.value, text:(e.innerText || e.textContent || '').trim(), disabled:e.disabled}))"
            )
            numeric_values = {
                value
                for option in options
                for value in (option["value"], option["text"])
                if value.isdigit() and 0 <= int(value) <= TICKET_LIMIT
            }
            if not {"0", "2"}.issubset(numeric_values):
                continue

            context = f"{descriptor} {nearby_text}"
            if adult_pattern.search(context):
                adult_selects.append(select)
            elif reduced_pattern.search(context):
                reduced_selects.append(select)
            elif re.search(r"(quantity|qty|anzahl|personen|tickets?)", descriptor):
                generic_quantity_selects.append(select)

        if adult_selects or generic_quantity_selects:
            break
        await page.wait_for_timeout(500)

    async def select_value(select: Locator, wanted_value: str) -> bool:
        options = await select.locator("option").evaluate_all(
            "els => els.map(e => ({value:e.value, text:(e.innerText || e.textContent || '').trim(), disabled:e.disabled}))"
        )
        option = next(
            (item for item in options
             if (item["value"] == wanted_value or item["text"] == wanted_value)
             and not item["disabled"]),
            None,
        )
        if option is None:
            return False
        await select.select_option(value=option["value"])
        return True

    if adult_selects:
        adult_set = await select_value(adult_selects[0], "2")
        for reduced_select in reduced_selects:
            if not await select_value(reduced_select, "0"):
                raise PageStateError("无法将优惠票数量设为 0")
        if adult_set:
            return

    for select in generic_quantity_selects:
        if await select_value(select, "2"):
            return

    raise PageStateError("无法识别购票数量下拉菜单，不能安全地设置两张票")


async def find_add_to_cart_button(page: Page) -> Optional[Locator]:
    """Find an enabled, visible cart action by its accessible text."""
    candidates = page.locator("button:visible, a:visible, input[type='submit']:visible")
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        try:
            label = " ".join((await candidate.inner_text(timeout=500)).split())
        except PlaywrightError:
            label = (await candidate.get_attribute("value") or "").strip()
        if re.search(r"in den warenkorb|add to cart|zum warenkorb", label, re.I):
            if await candidate.is_enabled():
                return candidate
    return None


def _normalize_title(value: str) -> str:
    return " ".join(value.split()).casefold()


async def cart_contains_product(context: BrowserContext, product_title: str) -> bool:
    """Check for this exact target title in the cart, preventing duplicates."""
    cart_page = await context.new_page()
    try:
        await cart_page.goto(CART_URL, wait_until="domcontentloaded", timeout=30_000)
        body = " ".join((await cart_page.locator("body").inner_text()).split())
        if re.search(r"(warenkorb|cart).{0,40}(leer|empty)|(?:leer|empty).{0,40}(warenkorb|cart)", body, re.I):
            return False
        titles = await cart_page.locator(
            "h1,h2,h3,h4,[class*=product-name],[class*=product-title],[class*=cart-item]"
        ).all_inner_texts()
        wanted = _normalize_title(product_title)
        return any(wanted in _normalize_title(title) for title in titles)
    finally:
        await cart_page.close()


async def add_product_to_cart(
    page: Page, context: BrowserContext, product_title: str
) -> tuple[str, str]:
    """Choose a ticket option, add two tickets, and verify the cart contents."""
    price_description = await choose_lowest_price(page)
    await set_quantity_to_two(page)
    await handle_cookie_consent(page)
    button = await find_add_to_cart_button(page)
    if button is None:
        raise PageStateError("未找到可用的“加入购物车”按钮")
    try:
        await button.click()
    except PlaywrightTimeoutError as click_error:
        # The consent layer can appear after the quantity controls have loaded.
        if not await page.locator("#usercentrics-root").count():
            raise
        await handle_cookie_consent(page)
        button = await find_add_to_cart_button(page)
        if button is None:
            raise PageStateError("关闭 Cookie 提示后，加入购物车按钮消失了") from click_error
        await button.click()
    await page.wait_for_timeout(1200)
    if not await cart_contains_product(context, product_title):
        raise PageStateError(
            "已点击加入购物车，但购物车中未确认找到目标商品"
        )
    return price_description, CART_URL


async def product_page_is_sold_out(page: Page) -> bool:
    """Detect AUSVERKAUFT only in the target product's heading container."""
    return await page.locator("body").evaluate(
        """body => {
          const h1 = body.querySelector('h1');
          if (!h1) return false;
          let node = h1.parentElement;
          for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
            const labels = [...node.querySelectorAll('[aria-label],[title],img[alt]')]
              .map(el => el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '')
              .join(' ');
            const text = `${node.innerText || ''} ${labels}`.replace(/\\s+/g, ' ').trim();
            const headings = node.querySelectorAll('h1,h2,h3,h4').length;
            if (headings <= 1 && text.length < 1600 && /\\bausverkauft\\b/i.test(text)) return true;
          }
          return false;
        }"""
    )


async def inspect_target_availability(page: Page, target: TicketTarget, settings: Settings) -> str:
    """Return 'sold_out', 'available', or 'unknown' for the locked target only."""
    await page.goto(target.listing_page_url, wait_until="domcontentloaded", timeout=30_000)
    await handle_cookie_consent(page)
    await handle_login_if_present(page, settings)

    if urlsplit(target.listing_page_url).path != urlsplit(target.product_url).path:
        listing_rows = await listing_titles_and_links(page)
        target_path = urlsplit(target.product_url).path.rstrip("/")
        matching_row = next(
            (
                (title, sold_out)
                for title, href, sold_out in listing_rows
                if urlsplit(href).path.rstrip("/") == target_path
            ),
            None,
        )
        if matching_row and matching_row[1]:
            return "sold_out"

    await page.goto(target.product_url, wait_until="domcontentloaded", timeout=30_000)
    await handle_cookie_consent(page)
    await handle_login_if_present(page, settings)
    if await product_page_is_sold_out(page):
        return "sold_out"
    if await find_add_to_cart_button(page):
        return "available"
    return "unknown"


def send_email(settings: Settings, product_title: str, price_description: str, cart_url: str) -> None:
    """Send a plain-text success notice through the configured SMTP server."""
    message = EmailMessage()
    message["Subject"] = f"Ticketfinder: {product_title} wurde zum Warenkorb hinzugefügt"
    message["From"] = settings.email_from
    message["To"] = settings.email_to
    message.set_content(
        f"Der passende Ticketartikel wurde zum Warenkorb hinzugefügt.\n\n"
        f"Artikel: {product_title}\n"
        f"Auswahl: {price_description}\n"
        f"Anzahl: {TICKET_LIMIT}\n"
        f"Warenkorb: {cart_url}\n\n"
        "Bitte öffne den Warenkorb im noch geöffneten Ticketfinder-Browserfenster "
        "und schließe die Bestellung dort selbst ab. Die Warenkorbsitzung wird "
        "nicht automatisch mit einem anderen Browserfenster geteilt."
    )

    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            _smtp_login(server, settings)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            _smtp_login(server, settings)
            server.send_message(message)


def send_sold_out_email(settings: Settings, product_title: str, product_url: str) -> None:
    """Send a one-time alert when the first matching product is sold out."""
    message = EmailMessage()
    message["Subject"] = f"Ticketfinder: {product_title} 当前已售罄"
    message["From"] = settings.email_from
    message["To"] = settings.email_to
    message.set_content(
        f"匹配到目标票务，但页面显示 AUSVERKAUFT（已售罄）。\n\n"
        f"商品：{product_title}\n"
        f"商品链接：{product_url}\n\n"
        f"脚本会继续每 {settings.interval} 秒检查该商品；如果恢复可购，会尝试加购并再次发送成功通知。"
    )
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            _smtp_login(server, settings)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            _smtp_login(server, settings)
            server.send_message(message)


def send_available_email(settings: Settings, product_title: str, product_url: str) -> None:
    """Notify that a matching ticket is available without adding it to the cart."""
    message = EmailMessage()
    message["Subject"] = f"Ticketfinder: 发现可购买票务 - {product_title}"
    message["From"] = settings.email_from
    message["To"] = settings.email_to
    message.set_content(
        f"Ticketfinder 检测到符合条件且当前可购买的票务。\n\n"
        f"商品：{product_title}\n"
        f"商品链接：{product_url}\n\n"
        "本次运行未使用 -c，因此程序没有将票务加入购物车。"
    )
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            _smtp_login(server, settings)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            _smtp_login(server, settings)
            server.send_message(message)


def _smtp_login(server: smtplib.SMTP, settings: Settings) -> None:
    if settings.smtp_username:
        if not settings.smtp_password:
            raise ConfigurationError("填写 SMTP_USERNAME 后还必须填写 SMTP_PASSWORD")
        server.login(settings.smtp_username, settings.smtp_password)


async def launch_browser(playwright: Any, *, headless: bool) -> Browser:
    """Launch Playwright's bundled Chromium, falling back to installed Google Chrome.

    The fallback is useful on older macOS releases where the current Playwright
    browser download may no longer be available, while Google Chrome is already
    installed and supported by Playwright.
    """
    try:
        return await playwright.chromium.launch(headless=headless)
    except Exception as bundled_error:
        LOG.warning(
            "Playwright 自带 Chromium 无法启动（%s），正在尝试使用已安装的 Google Chrome",
            bundled_error,
        )
        try:
            return await playwright.chromium.launch(channel="chrome", headless=headless)
        except Exception as chrome_error:
            raise RuntimeError(
                "Playwright 自带 Chromium 和 Google Chrome 均无法启动。请安装 Google Chrome 后重试。"
            ) from chrome_error


async def monitor(settings: Settings) -> None:
    """Poll indefinitely, recovering from isolated browser/page/network errors."""
    target: Optional[TicketTarget] = None
    sold_out_seen = False
    sold_out_notification_sent = False
    cart_confirmed = False
    notification_sent = False
    product_title = ", ".join(settings.keywords)
    price_description = "商品已存在于购物车"

    async with async_playwright() as playwright:
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None
        while True:
            try:
                if browser is None or not browser.is_connected():
                    browser = await launch_browser(playwright, headless=settings.headless)
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.set_default_timeout(8_000)
                    LOG.info("浏览器已启动，正在连接票务网站")
                    LOG.info("正在检查网站登录状态")

                assert context is not None and page is not None
                if target is None:
                    target = await discover_first_matching_product(
                        page, settings.product_url, settings.keywords, settings
                    )
                    if target is None:
                        LOG.info(
                            "已扫描所有相关分页，未找到标题同时包含全部关键词的票务：%s",
                            ", ".join(settings.keywords),
                        )
                    else:
                        product_title = target.title
                        sold_out_seen = target.sold_out_on_listing
                        LOG.info("已锁定分页顺序中第一个匹配项：%s", target.title)

                if target is not None and not cart_confirmed:
                    LOG.info("正在检查目标票务的库存状态：%s", target.title)
                    availability = await inspect_target_availability(page, target, settings)
                    if availability == "sold_out":
                        sold_out_seen = True
                        LOG.info("目标票务目前已售罄；将继续监控")
                    elif availability == "available":
                        LOG.info("目标票务当前可购买：%s", target.title)
                        if not settings.add_to_cart:
                            try:
                                send_available_email(settings, target.title, target.product_url)
                                LOG.info("购票提醒已发送至 %s；本次未加购，监控结束", settings.email_to)
                                return
                            except (OSError, smtplib.SMTPException):
                                LOG.exception("可购票务邮件发送失败，将在 %s 秒后重试", settings.interval)
                        else:
                            if await cart_contains_product(context, target.title):
                                cart_confirmed = True
                                price_description = "商品已存在于购物车"
                                LOG.info("目标票务已在购物车中，跳过重复加购")
                            else:
                                price_description, cart_url = await add_product_to_cart(page, context, target.title)
                                cart_confirmed = True
                                LOG.info("已将 %s 张票（%s）加入购物车", TICKET_LIMIT, price_description)
                                LOG.info("购物车链接：%s", cart_url)
                    else:
                        LOG.info("暂时无法确认目标票务库存，将稍后重试")

                if target is not None and sold_out_seen and not sold_out_notification_sent:
                    try:
                        send_sold_out_email(settings, target.title, target.product_url)
                        sold_out_notification_sent = True
                        LOG.info("售罄提醒已发送至 %s；本次运行不再重复发送", settings.email_to)
                    except (OSError, smtplib.SMTPException):
                        LOG.exception("售罄提醒邮件发送失败，将在 %s 秒后重试", settings.interval)

                if cart_confirmed and not notification_sent:
                    # The cart belongs to this Playwright browser context. Show
                    # it in the same visible window so the user can continue checkout.
                    await page.goto(CART_URL, wait_until="domcontentloaded", timeout=30_000)
                    await handle_cookie_consent(page)
                    await handle_login_if_present(page, settings)
                    send_email(settings, product_title, price_description, CART_URL)
                    notification_sent = True
                    LOG.info("加购成功提醒已发送至 %s", settings.email_to)
                    LOG.info("购物车链接：%s", CART_URL)
                    if not settings.headless:
                        LOG.info(
                            "购物车已在 Ticketfinder 浏览器窗口打开；请在该窗口继续结账，完成后回到终端按 Enter 关闭。"
                        )
                        try:
                            await asyncio.to_thread(
                            input,
                            "请在打开的浏览器窗口继续结账；完成后按 Enter 关闭浏览器。",
                            )
                        except EOFError:
                            LOG.warning("当前终端无法接收输入，将关闭浏览器")
                    return

            except (PlaywrightTimeoutError, PlaywrightError, PageStateError, OSError, smtplib.SMTPException) as exc:
                LOG.exception("本轮监控遇到错误，将在 %s 秒后重试：%s", settings.interval, exc)
                if isinstance(exc, PlaywrightError) and browser is not None:
                    try:
                        await browser.close()
                    except PlaywrightError:
                        pass
                    browser = context = page = None
            except Exception:
                # Keep the process alive for transient, unexpected page or SMTP errors.
                LOG.exception("发生未预期错误，将在 %s 秒后重试", settings.interval)
                if cart_confirmed:
                    notification_sent = False

            LOG.info("等待 %s 秒后继续刷新查找", settings.interval)
            await asyncio.sleep(settings.interval)


def main() -> int:
    """CLI entry point registered as the ``ticketfinder`` command."""
    args = parse_args()
    configure_logging()
    try:
        settings = load_settings(args)
        asyncio.run(_run(settings, args.scheduled_start))
    except ConfigurationError as exc:
        LOG.error("配置错误：%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("用户已停止程序")
        return 130
    return 0


async def _run(settings: Settings, start_at: Optional[datetime]) -> None:
    await wait_for_start(start_at)
    mode = "命中后加购并发送邮件" if settings.add_to_cart else "命中后仅发送邮件，不加购"
    LOG.info("开始监控：关键词=%s；刷新间隔=%s 秒；模式=%s", "、".join(settings.keywords), settings.interval, mode)
    await monitor(settings)


if __name__ == "__main__":
    raise SystemExit(main())
