"""
data/sentiment/ — Social sentiment scrapers.

  x.py         X.com (Twitter) posts via Playwright (no API required)
  reddit.py    Reddit incremental post + comment scraper
  telegram.py  Telegram channel/group message fetcher (Telethon)
  chan.py      4chan /biz/ board scraper (archive + live)

Each scraper outputs CSV or raw text for downstream sentiment scoring.
"""
