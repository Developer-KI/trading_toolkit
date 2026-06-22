"""
Merge pipeline for 4chan, Reddit, and Telegram CSV scrapers.

Reads every CSV in an input directory, detects the source by filename
prefix (4chan_, reddit_, telegram_), normalises each row into a shared
schema, and writes a single merged CSV.

Output columns
──────────────
source         – origin platform (4chan / reddit / telegram)
text_cleaned   – post or reply text with URLs and extra whitespace stripped
url            – link back to the original thread or message
author         – who wrote it (Anonymous for 4chan)
created_utc    – unix timestamp when available
score          – upvotes / reactions (0 when unavailable)
is_question    – 1 if the text ends with '?', 0 otherwise
thread_id      – groups replies with their parent thread
is_op          – 1 for the opening post, 0 for a reply
"""

import os
import re
import sys
import csv
from datetime import datetime
from typing import List, Dict, Optional
import pycld2 as cld2


import pandas as pd

# ───────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────
def detect_english_cld2(text) -> bool:
    """
    Use Google's Compact Language Detector 2 for better accuracy.
    """
    result = False
    try:
        details = cld2.detect(text)
        
        if details:
            # Check if English is among detected languages with 90% confidence
            for lang_code, percent, _, _ in details:
                if lang_code == 'en':
                    result = True
                    break
                
    except Exception as e:
       print(str(e))
    
    return result

def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"http\S+|www\S+|https\S+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_question(text: str) -> bool:
    return bool(re.search(r"\?\s*$", text))


REPLY_SEP = " ||| "

FIELDNAMES = [
    "source",
    "text_cleaned",
    "url",
    "author",
    "created_utc",
    "score",
    "is_question",
    "thread_id",
    "is_op",
]

DATE = datetime.now().strftime("%Y_%m_%d")

# ───────────────────────────────────────────
# 4chan
# ───────────────────────────────────────────

def process_4chan(file_path: str) -> List[Dict]:
    rows: List[Dict] = []
    df = pd.read_csv(file_path)

    for _, r in df.iterrows():
        thread_url = str(r.get("url", ""))
        thread_id = thread_url.rstrip("/").split("/")[-1] if thread_url else ""
        op_text = str(r.get("post", ""))

        rows.append(
            {
                "source": "4chan",
                "text_cleaned": clean_text(op_text),
                "url": thread_url,
                "author": "Anonymous",
                "created_utc": None,
                "score": 0,
                "is_question": int(is_question(op_text)),
                "thread_id": thread_id,
                "is_op": 1,
            }
        )

        replies_raw = str(r.get("replies", ""))
        if replies_raw and replies_raw != "nan":
            for reply in replies_raw.split(REPLY_SEP):
                reply = reply.strip()
                if not reply:
                    continue
                rows.append(
                    {
                        "source": "4chan",
                        "text_cleaned": clean_text(reply),
                        "url": thread_url,
                        "author": "Anonymous",
                        "created_utc": None,
                        "score": 0,
                        "is_question": int(is_question(reply)),
                        "thread_id": thread_id,
                        "is_op": 0,
                    }
                )

    return rows


# ───────────────────────────────────────────
# Reddit
# ───────────────────────────────────────────

def process_reddit(file_path: str) -> List[Dict]:
    rows: List[Dict] = []
    df = pd.read_csv(file_path)

    for _, r in df.iterrows():
        post_id = str(r.get("id", ""))
        post_text = str(r.get("post", ""))
        post_author = str(r.get("author", "")) if pd.notna(r.get("author")) else ""
        post_created = r.get("created_utc") if pd.notna(r.get("created_utc")) else None
        post_score = r.get("score") if pd.notna(r.get("score")) else 0
        post_url = str(r.get("url", ""))

        rows.append(
            {
                "source": "reddit",
                "text_cleaned": clean_text(post_text),
                "url": post_url,
                "author": post_author,
                "created_utc": post_created,
                "score": post_score,
                "is_question": int(is_question(post_text)),
                "thread_id": post_id,
                "is_op": 1,
            }
        )

        replies_raw = str(r.get("replies", ""))
        if replies_raw and replies_raw != "nan":
            for reply in replies_raw.split(REPLY_SEP):
                reply = reply.strip()
                if not reply:
                    continue
                rows.append(
                    {
                        "source": "reddit",
                        "text_cleaned": clean_text(reply),
                        "url": post_url,
                        "author": None,
                        "created_utc": None,
                        "score": None,
                        "is_question": int(is_question(reply)),
                        "thread_id": post_id,
                        "is_op": 0,
                    }
                )

    return rows


# ───────────────────────────────────────────
# Telegram
# ───────────────────────────────────────────

def process_telegram(file_path: str) -> List[Dict]:
    rows: List[Dict] = []
    fname = os.path.basename(file_path)

    channel = fname
    if channel.startswith("telegram_"):
        channel = channel[len("telegram_"):]
    channel = re.sub(r"_\d{4}_\d{2}_\d{2}_\d{2}_\d{2}\.csv$", "", channel)

    df = pd.read_csv(file_path)

    for _, r in df.iterrows():
        msg_text = str(r.get("text", "")) if pd.notna(r.get("text")) else ""
        msg_date = str(r.get("date", "")) if pd.notna(r.get("date")) else None

        created_utc = None
        if msg_date:
            try:
                created_utc = datetime.fromisoformat(msg_date).timestamp()
            except (ValueError, TypeError):
                pass

        urls = str(r.get("urls", "")) if pd.notna(r.get("urls")) else ""

        rows.append(
            {
                "source": "telegram",
                "text_cleaned": clean_text(msg_text),
                "url": urls,
                "author": str(r.get("sender_id", "")) if pd.notna(r.get("sender_id")) else None,
                "created_utc": created_utc,
                "score": 0,
                "is_question": int(is_question(msg_text)) if msg_text else 0,
                "thread_id": channel,
                "is_op": 1,
            }
        )

    return rows


# ───────────────────────────────────────────
# Main
# ───────────────────────────────────────────

PROCESSORS = {
    "4chan_": process_4chan,
    "reddit_": process_reddit,
    "telegram_": process_telegram,
}


def process_data(input_dir: str, output_dir: str, filename: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    output = os.path.join(output_dir, filename)

    all_rows: List[Dict] = []

    for fname in sorted(os.listdir(input_dir)):
        if not fname.endswith(".csv"):
            continue

        filepath = os.path.join(input_dir, fname)
        print(f"Processing {fname}...", file=sys.stderr)

        matched = False
        for prefix, processor in PROCESSORS.items():
            if fname.startswith(prefix) and fname.endswith(f'{DATE}.csv'):
                rows = processor(filepath)
                print(f"  -> {len(rows)} rows from {prefix.rstrip('_')}", file=sys.stderr)
                all_rows.extend(rows)
                matched = True
                break

        if not matched:
            print(f"  -> Skipping (unknown prefix)", file=sys.stderr)

    print(f"\nTotal rows: {len(all_rows)}", file=sys.stderr)

    with open(output, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"CSV written to {output}", file=sys.stderr)
    return output

if __name__ == "__main__":
    FILENAME = f'sentiment_{DATE}.csv'
    process_data(input_dir="./datasets", output_dir='./datasets/sentiment', filename=FILENAME)