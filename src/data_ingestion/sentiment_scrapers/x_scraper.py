"""
X (Twitter) Web Scraper — No Official API Required
====================================================
Scrapes posts from X.com using Playwright (browser automation) + BeautifulSoup.

This approach renders X's JavaScript-heavy pages in a real browser, then
parses the resulting HTML. It does NOT use the official paid API.

Requirements:
    pip install playwright beautifulsoup4 pandas lxml
    playwright install chromium

⚠️  IMPORTANT DISCLAIMERS:
    - X's Terms of Service prohibit automated scraping. Use at your own risk.
    - X actively detects and blocks bots. This scraper may stop working at any time.
    - X's HTML structure changes frequently; selectors may need updating.
    - Respect rate limits. Don't hammer the site.
    - In the EU, GDPR applies to personal data you collect. Be careful.
    - This is for educational / personal research purposes.

Usage:
    python x_web_scraper.py
"""

import asyncio
import json
import os
import re
import time
import logging
import random
import pandas as pd
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print(
        "Playwright is not installed. Run:\n"
        "  pip install playwright\n"
        "  playwright install chromium"
    )
    raise

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

load_dotenv()

# X login credentials (required — X redirects logged-out users to login walls)
X_USERNAME = os.getenv("X_USERNAME", "")
X_PASSWORD = os.getenv("X_PASSWORD", "")
# Email or phone for the "verify your identity" step X sometimes shows
X_EMAIL = os.getenv("X_EMAIL", "")

# What to scrape
HASHTAGS = ["python", "machinelearning"]
USERS = ["OpenAI", "elikiloApp"]

# How many posts to collect per target (approximate — scroll-based)
MAX_POSTS_PER_TARGET = 50

# Scroll behavior
SCROLL_PAUSE_MIN = 2.0   # Minimum seconds between scrolls
SCROLL_PAUSE_MAX = 4.0   # Maximum seconds between scrolls (randomized to look human)
MAX_SCROLLS = 25          # Safety cap on scroll attempts per page

# Browser settings
HEADLESS = False          # Set to True once login works reliably
SLOW_MO = 100             # Milliseconds to slow down actions (helps avoid detection)

# Output
OUTPUT_DIR = "output"
DEBUG_DIR = "debug"       # Screenshots saved here on login failure

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# SCRAPER CLASS
# ──────────────────────────────────────────────

class XWebScraper:
    """
    Scrapes X.com posts using Playwright browser automation + BeautifulSoup parsing.

    Strategy:
        1. Launch a real Chromium browser via Playwright
        2. Log in to X (required since X blocks most content for logged-out users)
        3. Navigate to search/profile URLs
        4. Scroll to load posts dynamically
        5. Parse the rendered HTML with BeautifulSoup
        6. Extract post data from the DOM
    """

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.all_posts = []
        self.seen_ids = set()

    async def start(self):
        """Launch browser and create page."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=HEADLESS,
            slow_mo=SLOW_MO,
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        self.page = await self.context.new_page()
        logger.info("Browser launched.")

    async def enable_resource_blocking(self):
        """Block images/videos/fonts to speed up scraping. Call AFTER login."""
        await self.page.route(
            re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|woff2?|mp4|m3u8)$"),
            lambda route: route.abort(),
        )
        logger.info("Resource blocking enabled (images/video/fonts).")

    async def close(self):
        """Clean up browser resources."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser closed.")

    # ──────────────────────────────────────────
    # LOGIN
    # ──────────────────────────────────────────

    async def login(self, username: str, password: str, email: str = ""):
        """
        Log in to X.com.

        X's login flow has multiple steps and may request email/phone
        verification or CAPTCHAs. This handles the basic username → password
        flow plus the common "verify your identity" intermediate step.

        If login fails, a screenshot is saved to DEBUG_DIR so you can see
        exactly what the browser was showing at the point of failure.
        """
        if not username or not password:
            raise ValueError(
                "X login credentials required. X blocks most content for "
                "logged-out users.\n"
                "Set via environment variables:\n"
                "  export X_USERNAME='your_username'\n"
                "  export X_PASSWORD='your_password'\n"
                "  export X_EMAIL='your_email'  (optional, for verification step)\n"
                "Or edit the config in this file."
            )

        Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
        logger.info(f"Logging in as @{username}...")

        # ── Navigate to login page ──
        await self.page.goto("https://x.com/i/flow/login", wait_until="networkidle")
        await self._random_delay(3, 5)

        # ── Step 1: Enter username ──
        username_input = await self._find_first_selector([
            'input[autocomplete="username"]',
            'input[name="text"]',
            'input[type="text"]',
        ], timeout=20000, step_name="username input")
        if not username_input:
            return False

        await username_input.click()
        await self._random_delay(0.3, 0.6)
        await username_input.fill("")  # Clear first
        await self._type_human(username_input, username)
        await self._random_delay(0.5, 1.0)

        # Click "Next" — try multiple strategies
        next_clicked = await self._click_button_by_strategies([
            ('css', 'button[role="button"]:has-text("Next")'),
            ('css', 'div[role="button"]:has-text("Next")'),
            ('text', 'Next'),
        ], step_name="Next button")
        if not next_clicked:
            return False

        await self._random_delay(2, 4)

        # ── Step 1.5: Handle "verify your identity" screen ──
        # X sometimes asks you to confirm your email or phone number
        await self._handle_verification_step(username, email)

        # ── Step 2: Enter password ──
        password_input = await self._find_first_selector([
            'input[name="password"]',
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ], timeout=20000, step_name="password input")
        if not password_input:
            return False

        await password_input.click()
        await self._random_delay(0.3, 0.6)
        await self._type_human(password_input, password)
        await self._random_delay(0.5, 1.0)

        # Click "Log in" — try multiple strategies
        login_clicked = await self._click_button_by_strategies([
            ('css', 'button[data-testid="LoginForm_Login_Button"]'),
            ('css', 'button[role="button"]:has-text("Log in")'),
            ('css', 'div[role="button"]:has-text("Log in")'),
            ('text', 'Log in'),
        ], step_name="Log in button")
        if not login_clicked:
            return False

        await self._random_delay(4, 6)

        # ── Step 3: Verify login succeeded ──
        success = await self._verify_login()
        if not success:
            # One more check — sometimes there's another verification screen
            await self._handle_verification_step(username, email)
            await self._random_delay(2, 3)
            success = await self._verify_login()

        return success

    async def _find_first_selector(
        self, selectors: list[str], timeout: int = 15000, step_name: str = ""
    ):
        """
        Try multiple CSS selectors and return the first one that matches.
        Saves a debug screenshot if none are found.
        """
        for selector in selectors:
            try:
                el = await self.page.wait_for_selector(selector, timeout=timeout)
                if el:
                    logger.debug(f"  Found {step_name} via: {selector}")
                    return el
            except PWTimeout:
                continue

        # All selectors failed — save debug screenshot
        screenshot_path = os.path.join(
            DEBUG_DIR, f"fail_{step_name.replace(' ', '_')}_{int(time.time())}.png"
        )
        await self.page.screenshot(path=screenshot_path, full_page=True)
        logger.error(
            f"Could not find {step_name}. None of these selectors matched:\n"
            + "\n".join(f"    {s}" for s in selectors)
            + f"\n  Screenshot saved → {screenshot_path}"
            + "\n  Try running with HEADLESS = False to see the page."
        )
        return None

    async def _click_button_by_strategies(
        self, strategies: list[tuple[str, str]], step_name: str = ""
    ) -> bool:
        """
        Try multiple strategies to find and click a button.
        Each strategy is a (method, value) tuple:
            ('css', 'button[data-testid="..."]')
            ('text', 'Next')
        """
        for method, value in strategies:
            try:
                if method == "css":
                    btn = await self.page.wait_for_selector(value, timeout=5000)
                    if btn:
                        await btn.click()
                        logger.debug(f"  Clicked {step_name} via CSS: {value}")
                        return True
                elif method == "text":
                    locator = self.page.get_by_text(value, exact=True)
                    if await locator.count() > 0:
                        await locator.first.click()
                        logger.debug(f"  Clicked {step_name} via text: {value}")
                        return True
            except (PWTimeout, Exception):
                continue

        # All strategies failed
        screenshot_path = os.path.join(
            DEBUG_DIR, f"fail_{step_name.replace(' ', '_')}_{int(time.time())}.png"
        )
        await self.page.screenshot(path=screenshot_path, full_page=True)
        logger.error(
            f"Could not click {step_name}. All strategies failed.\n"
            f"  Screenshot saved → {screenshot_path}"
        )
        return False

    async def _handle_verification_step(self, username: str, email: str):
        """
        Handle the 'verify your identity' intermediate screen.
        X may ask for your email, phone, or username to confirm identity.
        """
        await self._random_delay(1, 2)

        # Check for the verification input — multiple possible selectors
        verification_input = None
        for selector in [
            'input[data-testid="ocfEnterTextTextInput"]',
            'input[name="text"]',
        ]:
            try:
                el = await self.page.wait_for_selector(selector, timeout=3000)
                # Make sure this isn't the password field
                input_type = await el.get_attribute("type")
                if input_type == "password":
                    continue

                # Check if we're on a verification screen by looking at surrounding text
                page_text = await self.page.inner_text("body")
                verification_keywords = [
                    "verify", "confirm", "enter your phone",
                    "enter your email", "unusual login", "identity",
                ]
                if any(kw in page_text.lower() for kw in verification_keywords):
                    verification_input = el
                    break
            except (PWTimeout, Exception):
                continue

        if not verification_input:
            return  # No verification screen, continue normally

        logger.warning("X is asking for identity verification.")

        # Determine what to enter: email if provided, otherwise username
        verification_value = email or username
        logger.info(f"  Entering verification: {verification_value}")

        await verification_input.click()
        await self._random_delay(0.3, 0.6)
        await self._type_human(verification_input, verification_value)
        await self._random_delay(0.5, 1.0)

        # Click the confirm/next button
        await self._click_button_by_strategies([
            ('css', 'button[data-testid="ocfEnterTextNextButton"]'),
            ('css', 'button[role="button"]:has-text("Next")'),
            ('text', 'Next'),
            ('text', 'Verify'),
        ], step_name="verification confirm")

        await self._random_delay(2, 4)

    async def _verify_login(self) -> bool:
        """Check if we successfully landed on the home feed."""
        for selector in [
            'a[data-testid="AppTabBar_Home_Link"]',
            'a[aria-label="Home"]',
            'a[href="/home"]',
            'nav[aria-label="Primary"]',
        ]:
            try:
                await self.page.wait_for_selector(selector, timeout=5000)
                logger.info("Login successful!")
                return True
            except PWTimeout:
                continue

        # Check if we're on the home timeline by URL
        if "/home" in self.page.url:
            logger.info("Login successful! (detected via URL)")
            return True

        screenshot_path = os.path.join(DEBUG_DIR, f"fail_login_verify_{int(time.time())}.png")
        await self.page.screenshot(path=screenshot_path, full_page=True)
        logger.error(
            "Login may have failed — could not confirm home feed.\n"
            f"  Current URL: {self.page.url}\n"
            f"  Screenshot saved → {screenshot_path}\n"
            "  Possible causes: wrong credentials, CAPTCHA, 2FA, or "
            "additional verification.\n"
            "  Run with HEADLESS = False to see what's happening."
        )
        return False

    async def _type_human(self, element, text: str):
        """Type text character by character with random delays to mimic human input."""
        for char in text:
            await element.type(char, delay=random.randint(30, 120))
            await asyncio.sleep(random.uniform(0.01, 0.05))

    # ──────────────────────────────────────────
    # SCRAPE BY HASHTAG
    # ──────────────────────────────────────────

    async def scrape_hashtag(self, hashtag: str, max_posts: int = MAX_POSTS_PER_TARGET) -> list[dict]:
        """
        Scrape posts for a given hashtag using X's search.

        Args:
            hashtag: The hashtag to search (without #)
            max_posts: Approximate max number of posts to collect

        Returns:
            List of post dictionaries
        """
        search_url = f"https://x.com/search?q=%23{hashtag}&src=typed_query&f=live"
        logger.info(f"Scraping hashtag: #{hashtag}")
        return await self._scrape_page(search_url, max_posts, label=f"#{hashtag}")

    # ──────────────────────────────────────────
    # SCRAPE BY USER
    # ──────────────────────────────────────────

    async def scrape_user(self, username: str, max_posts: int = MAX_POSTS_PER_TARGET) -> list[dict]:
        """
        Scrape posts from a specific user's profile.

        Args:
            username: The username (without @)
            max_posts: Approximate max number of posts to collect

        Returns:
            List of post dictionaries
        """
        profile_url = f"https://x.com/{username}"
        logger.info(f"Scraping user: @{username}")
        return await self._scrape_page(profile_url, max_posts, label=f"@{username}")

    # ──────────────────────────────────────────
    # CORE SCROLL + PARSE LOGIC
    # ──────────────────────────────────────────

    async def _scrape_page(self, url: str, max_posts: int, label: str = "") -> list[dict]:
        """
        Navigate to a URL, scroll to load posts, and parse them.

        X loads posts dynamically as you scroll. This method scrolls
        repeatedly, parses new content each time, and stops when we've
        collected enough posts or run out of new content.
        """
        posts = []
        local_seen = set()

        await self.page.goto(url, wait_until="domcontentloaded")
        await self._random_delay(3, 5)

        scroll_count = 0
        stale_rounds = 0  # Tracks consecutive scrolls with no new posts

        while len(posts) < max_posts and scroll_count < MAX_SCROLLS:
            # Get current page HTML and parse it
            html = await self.page.content()
            new_posts = self._parse_posts_from_html(html, local_seen)

            if new_posts:
                posts.extend(new_posts)
                stale_rounds = 0
                logger.info(f"  [{label}] Scroll {scroll_count + 1}: +{len(new_posts)} posts (total: {len(posts)})")
            else:
                stale_rounds += 1
                if stale_rounds >= 3:
                    logger.info(f"  [{label}] No new posts for 3 scrolls, stopping.")
                    break

            # Scroll down
            await self.page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await self._random_delay(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX)
            scroll_count += 1

        logger.info(f"  [{label}] Done. Collected {len(posts)} posts.")
        return posts[:max_posts]

    def _parse_posts_from_html(self, html: str, seen_ids: set) -> list[dict]:
        """
        Parse post data from raw HTML using BeautifulSoup.

        ⚠️  These CSS selectors are based on X's current DOM structure and
        WILL break when X updates their frontend. When that happens, inspect
        the page in DevTools and update the selectors below.
        """
        soup = BeautifulSoup(html, "lxml")
        new_posts = []

        # X renders each tweet inside an <article> element with data-testid="tweet"
        articles = soup.find_all("article", attrs={"data-testid": "tweet"})

        for article in articles:
            try:
                post = self._extract_post_from_article(article)
                if post and post["id"] not in seen_ids and post["id"] not in self.seen_ids:
                    seen_ids.add(post["id"])
                    self.seen_ids.add(post["id"])
                    new_posts.append(post)
            except Exception as e:
                logger.debug(f"  Failed to parse an article: {e}")
                continue

        return new_posts

    def _extract_post_from_article(self, article) -> dict | None:
        """
        Extract structured data from a single tweet <article> element.

        This is the most fragile part — X changes class names and structure
        frequently. The approach uses data-testid attributes where possible
        since those are more stable than CSS classes.
        """
        # ── Post URL and ID ──
        # Look for the timestamp link which contains the post URL
        time_el = article.find("time")
        if not time_el:
            return None

        time_link = time_el.find_parent("a")
        post_url = ""
        post_id = ""
        if time_link and time_link.get("href"):
            href = time_link["href"]
            post_url = f"https://x.com{href}"
            # Extract post ID from URL: /username/status/1234567890
            match = re.search(r"/status/(\d+)", href)
            if match:
                post_id = match.group(1)

        if not post_id:
            return None

        # ── Timestamp ──
        created_at = time_el.get("datetime", "")

        # ── Author info ──
        # User info is in the header area of the tweet
        user_links = article.find_all("a", attrs={"role": "link"})
        author_username = ""
        author_name = ""

        for link in user_links:
            href = link.get("href", "")
            # Username links look like "/username" (no /status/ etc.)
            if href and re.match(r"^/[A-Za-z0-9_]+$", href) and "/status/" not in href:
                author_username = href.strip("/")
                # The display name is typically nearby
                name_spans = link.find_all("span")
                for span in name_spans:
                    text = span.get_text(strip=True)
                    if text and text != f"@{author_username}" and not text.startswith("@"):
                        author_name = text
                        break
                break

        # ── Post text ──
        text_el = article.find("div", attrs={"data-testid": "tweetText"})
        text = text_el.get_text(separator=" ", strip=True) if text_el else ""

        # ── Engagement metrics ──
        # X uses aria-label on group buttons for metrics
        likes = self._extract_metric(article, "like")
        retweets = self._extract_metric(article, "retweet") + self._extract_metric(article, "repost")
        replies = self._extract_metric(article, "repl")
        bookmarks = self._extract_metric(article, "bookmark")
        views = self._extract_views(article)

        # ── Hashtags ──
        hashtags = []
        if text_el:
            for a_tag in text_el.find_all("a"):
                href = a_tag.get("href", "")
                if "/hashtag/" in href:
                    tag_text = a_tag.get_text(strip=True).lstrip("#")
                    hashtags.append(tag_text)

        # ── URLs ──
        urls = []
        if text_el:
            for a_tag in text_el.find_all("a"):
                href = a_tag.get("href", "")
                if href.startswith("http") and "x.com" not in href and "twitter.com" not in href:
                    urls.append(href)

        return {
            "id": post_id,
            "text": text,
            "author_username": author_username,
            "author_name": author_name,
            "created_at": created_at,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "bookmarks": bookmarks,
            "views": views,
            "hashtags": ", ".join(hashtags),
            "urls": ", ".join(urls),
            "post_url": post_url,
        }

    def _extract_metric(self, article, keyword: str) -> int:
        """
        Extract an engagement metric by searching aria-labels.

        X buttons have aria-labels like "42 Likes", "3 replies", etc.
        """
        buttons = article.find_all("button", attrs={"aria-label": True})
        for btn in buttons:
            label = btn.get("aria-label", "").lower()
            if keyword in label:
                match = re.search(r"([\d,]+)", label)
                if match:
                    return int(match.group(1).replace(",", ""))
        return 0

    def _extract_views(self, article) -> int:
        """Extract view count from the analytics link."""
        analytics_link = article.find("a", attrs={"aria-label": True})
        # Views are sometimes in an aria-label containing "view"
        all_links = article.find_all("a", attrs={"aria-label": True})
        for link in all_links:
            label = link.get("aria-label", "").lower()
            if "view" in label:
                match = re.search(r"([\d,]+)", label)
                if match:
                    return int(match.group(1).replace(",", ""))
        return 0

    # ──────────────────────────────────────────
    # UTILITIES
    # ──────────────────────────────────────────

    async def _random_delay(self, min_sec: float, max_sec: float):
        """Sleep for a random duration to mimic human behavior."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    # ──────────────────────────────────────────
    # EXPORT
    # ──────────────────────────────────────────

    @staticmethod
    def export_to_csv(posts: list[dict], filename: str = "posts.csv") -> str:
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        filepath = os.path.join(OUTPUT_DIR, filename)
        df = pd.DataFrame(posts)
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        logger.info(f"Exported {len(posts)} posts → {filepath}")
        return filepath

    @staticmethod
    def export_to_json(posts: list[dict], filename: str = "posts.json") -> str:
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(posts, f, indent=2, ensure_ascii=False, default=str)
        logger.info(f"Exported {len(posts)} posts → {filepath}")
        return filepath

    @staticmethod
    def print_summary(posts: list[dict]):
        if not posts:
            print("\nNo posts scraped.")
            return

        df = pd.DataFrame(posts)
        print("\n" + "=" * 60)
        print("  SCRAPE SUMMARY")
        print("=" * 60)
        print(f"  Total posts collected: {len(posts)}")
        print(f"  Unique authors:        {df['author_username'].nunique()}")
        print(f"  Total likes:           {df['likes'].sum():,}")
        print(f"  Total retweets:        {df['retweets'].sum():,}")

        if df["created_at"].notna().any() and (df["created_at"] != "").any():
            valid = df[df["created_at"] != ""]
            print(f"  Date range:            {valid['created_at'].min()} → {valid['created_at'].max()}")

        print("\n  Top 5 most-liked posts:")
        top = df.nlargest(5, "likes")[["author_username", "text", "likes"]]
        for _, row in top.iterrows():
            preview = row["text"][:80].replace("\n", " ") + "..."
            print(f"    @{row['author_username']} ({row['likes']} likes): {preview}")
        print("=" * 60)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

async def main():
    scraper = XWebScraper()

    try:
        await scraper.start()

        # ── Login (required) ──
        logged_in = await scraper.login(X_USERNAME, X_PASSWORD, X_EMAIL)
        if not logged_in:
            logger.error("Login failed. Check debug/ folder for screenshots.")
            return

        # Enable resource blocking now that login is done (speeds up scraping)
        await scraper.enable_resource_blocking()

        all_posts = []

        # ── Scrape hashtags ──
        for tag in HASHTAGS:
            posts = await scraper.scrape_hashtag(tag, max_posts=MAX_POSTS_PER_TARGET)
            all_posts.extend(posts)
            await scraper._random_delay(3, 6)  # Pause between targets

        # ── Scrape users ──
        for user in USERS:
            posts = await scraper.scrape_user(user, max_posts=MAX_POSTS_PER_TARGET)
            all_posts.extend(posts)
            await scraper._random_delay(3, 6)

        # ── Deduplicate ──
        seen = set()
        unique = []
        for p in all_posts:
            if p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)

        # ── Export ──
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        scraper.export_to_csv(unique, f"x_scraped_{ts}.csv")
        scraper.export_to_json(unique, f"x_scraped_{ts}.json")
        scraper.print_summary(unique)

    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())