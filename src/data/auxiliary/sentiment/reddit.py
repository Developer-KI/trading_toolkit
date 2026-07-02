import os
import re
import csv
import json
import time
import requests
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any, Optional


class RedditScraper:
    def __init__(self, user_agent: str = "Reddit Scraper Bot v1.0"):
        self.base_url = "https://www.reddit.com"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # ──────────────────────────────────────────
    # MARKER (incremental scraping)
    # ──────────────────────────────────────────

    def _marker_path(self, subreddit: str) -> str:
        os.makedirs("./datasets", exist_ok=True)
        return f"./datasets/.last_seen_{subreddit}.json"

    def _load_last_seen_id(self, subreddit: str) -> Optional[str]:
        path = self._marker_path(subreddit)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f).get("last_seen_id")
        except (json.JSONDecodeError, IOError):
            return None

    def _save_last_seen_id(self, subreddit: str, post_id: str) -> None:
        path = self._marker_path(subreddit)
        with open(path, "w") as f:
            json.dump(
                {
                    "last_seen_id": post_id,
                    "updated_at": datetime.now().isoformat(),
                    "subreddit": subreddit,
                },
                f,
                indent=2,
            )

    # ──────────────────────────────────────────
    # CSV HELPERS
    # ──────────────────────────────────────────

    # Shared columns with the 4chan scraper come first so the two
    # datasets can be concatenated with pd.concat without alignment
    # headaches.  Extra Reddit-specific columns follow.
    CSV_COLUMNS = [
        # ── shared with 4chan ──
        "post",          # selftext (4chan: OP text)
        "replies",       # top-level comment bodies joined with " ||| "
        "reply_count",   # number of top-level comments scraped
        "category",      # "question" / "none_question"
        "url",           # permalink to the post
        # ── reddit extras ──
        "source",        # always "reddit" (handy after merging)
        "id",
        "title",
        "author",
        "created_utc",
        "created_date",
        "score",
        "upvote_ratio",
        "num_comments",  # Reddit's own comment count
        "subreddit",
        "is_self",
        "over_18",
        "spoiler",
        "stickied",
    ]

    @staticmethod
    def _is_question(text: str) -> bool:
        """Simple heuristic shared with the 4chan scraper."""
        return bool(re.search(r"\?\s*$", text))

    def _flatten_replies(self, comments: List[Dict]) -> str:
        """Join top-level comment bodies with the same delimiter the
        4chan scraper uses, so downstream code can split identically."""
        bodies = [c.get("body", "") for c in comments if c.get("body")]
        return " ||| ".join(bodies)

    def _post_to_row(self, post: Dict, subreddit: str) -> Dict:
        """Convert a rich post dict into a flat CSV row."""
        text = post.get("selftext", "") or post.get("title", "")
        comments = post.get("comments", [])
        return {
            "post": text,
            "replies": self._flatten_replies(comments),
            "reply_count": len(comments),
            "category": "question" if self._is_question(text) else "none_question",
            "url": f"https://www.reddit.com{post.get('permalink', '')}",
            "source": "reddit",
            "id": post.get("id"),
            "title": post.get("title"),
            "author": post.get("author"),
            "created_utc": post.get("created_utc"),
            "created_date": post.get("created_date"),
            "score": post.get("score"),
            "upvote_ratio": post.get("upvote_ratio"),
            "num_comments": post.get("num_comments"),
            "subreddit": subreddit,
            "is_self": post.get("is_self", False),
            "over_18": post.get("over_18", False),
            "spoiler": post.get("spoiler", False),
            "stickied": post.get("stickied", False),
        }

    def _save_csv(self, rows: List[Dict], path: str) -> None:
        """Write (or overwrite) the dataset CSV."""
        df = pd.DataFrame(rows, columns=self.CSV_COLUMNS)
        df.to_csv(path, index=False)

    # ──────────────────────────────────────────
    # REDDIT API HELPERS
    # ──────────────────────────────────────────

    def _fetch_new_page(
        self,
        subreddit: str,
        after: Optional[str] = None,
        count: int = 0,
        limit: int = 100,
    ) -> Dict:
        url = f"{self.base_url}/r/{subreddit}/new.json"
        params = {"limit": min(limit, 100), "raw_json": 1}
        if after:
            params["after"] = after
            params["count"] = count
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def get_subreddit_posts(
        self,
        subreddit: str,
        limit: int = 25,
        sort: str = "hot",
        time_filter: str = "all",
    ) -> List[Dict]:
        url = f"{self.base_url}/r/{subreddit}/{sort}.json"
        params = {"limit": min(limit, 100), "raw_json": 1}
        if sort == "top":
            params["t"] = time_filter
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return [self._process_post(p["data"]) for p in data["data"]["children"]]
        except requests.exceptions.RequestException as e:
            print(f"Error fetching posts: {e}")
            return []

    def get_post_comments(
        self, subreddit: str, post_id: str, limit: int = 100
    ) -> List[Dict]:
        url = f"{self.base_url}/r/{subreddit}/comments/{post_id}.json"
        params = {"limit": limit, "raw_json": 1, "sort": "top"}
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if len(data) < 2:
                return []
            return [
                self._process_comment(c["data"])
                for c in data[1]["data"]["children"]
                if c["kind"] != "more"
            ]
        except requests.exceptions.RequestException as e:
            print(f"Error fetching comments for post {post_id}: {e}")
            return []

    # ──────────────────────────────────────────
    # DATA PROCESSING
    # ──────────────────────────────────────────

    def _process_post(self, post_data: Dict) -> Dict:
        return {
            "id": post_data.get("id"),
            "title": post_data.get("title"),
            "author": post_data.get("author"),
            "created_utc": post_data.get("created_utc"),
            "created_date": datetime.fromtimestamp(
                post_data.get("created_utc", 0)
            ).isoformat(),
            "score": post_data.get("score"),
            "upvote_ratio": post_data.get("upvote_ratio"),
            "num_comments": post_data.get("num_comments"),
            "permalink": post_data.get("permalink"),
            "url": post_data.get("url"),
            "selftext": post_data.get("selftext", ""),
            "is_self": post_data.get("is_self", False),
            "over_18": post_data.get("over_18", False),
            "spoiler": post_data.get("spoiler", False),
            "stickied": post_data.get("stickied", False),
            "comments": [],
        }

    def _process_comment(self, comment_data: Dict) -> Dict:
        return {
            "id": comment_data.get("id"),
            "author": comment_data.get("author"),
            "created_utc": comment_data.get("created_utc"),
            "created_date": datetime.fromtimestamp(
                comment_data.get("created_utc", 0)
            ).isoformat(),
            "score": comment_data.get("score"),
            "body": comment_data.get("body", ""),
            "permalink": comment_data.get("permalink"),
            "parent_id": comment_data.get("parent_id"),
            "stickied": comment_data.get("stickied", False),
            "replies": self._process_replies(comment_data.get("replies", {})),
        }

    def _process_replies(self, replies_data: Any) -> List[Dict]:
        replies = []
        if not replies_data or not isinstance(replies_data, dict):
            return replies
        try:
            children = replies_data.get("data", {}).get("children", [])
            for child in children:
                if child["kind"] != "more":
                    rd = child["data"]
                    replies.append(
                        {
                            "id": rd.get("id"),
                            "author": rd.get("author"),
                            "created_utc": rd.get("created_utc"),
                            "created_date": datetime.fromtimestamp(
                                rd.get("created_utc", 0)
                            ).isoformat(),
                            "score": rd.get("score"),
                            "body": rd.get("body", ""),
                            "permalink": rd.get("permalink"),
                            "parent_id": rd.get("parent_id"),
                            "stickied": rd.get("stickied", False),
                            "replies": self._process_replies(
                                rd.get("replies", {})
                            ),
                        }
                    )
        except (KeyError, AttributeError, TypeError) as e:
            print(f"Error processing replies: {e}")
        return replies

    # ──────────────────────────────────────────
    # PUBLIC SCRAPE METHODS
    # ──────────────────────────────────────────

    def scrape_new_until_seen(
        self,
        subreddit: str,
        comments_per_post: int = 50,
        max_posts: int = 5000,
        page_size: int = 100,
        delay: float = 1.0,
        save_every: int = 50,
        filename: Optional[str] = None,
    ) -> List[Dict]:
        """Incremental scrape of /new — stops when it hits the last-seen post."""

        last_seen_id = self._load_last_seen_id(subreddit)

        if last_seen_id:
            print(
                f"Incremental scrape of r/{subreddit} — "
                f"will stop at post {last_seen_id}"
            )
        else:
            print(
                f"First scrape of r/{subreddit} — "
                f"will fetch up to {max_posts} newest posts"
            )

        new_posts: List[Dict] = []
        after_cursor: Optional[str] = None
        fetched_count = 0
        hit_seen = False

        while fetched_count < max_posts:
            try:
                page = self._fetch_new_page(
                    subreddit,
                    after=after_cursor,
                    count=fetched_count,
                    limit=page_size,
                )
            except requests.exceptions.RequestException as e:
                print(f"Request error: {e}")
                break

            children = page.get("data", {}).get("children", [])
            if not children:
                print("No more posts returned by Reddit.")
                break

            for child in children:
                post_data = child["data"]
                post_id = post_data.get("id")

                if post_id == last_seen_id:
                    print(f"Reached already-seen post {post_id} — stopping.")
                    hit_seen = True
                    break

                new_posts.append(self._process_post(post_data))
                fetched_count += 1

                if fetched_count >= max_posts:
                    print(f"Reached max_posts cap ({max_posts}).")
                    break

            if hit_seen or fetched_count >= max_posts:
                break

            after_cursor = page.get("data", {}).get("after")
            if not after_cursor:
                print("Reddit returned no 'after' cursor — end of listing.")
                break

            print(f"  Fetched {fetched_count} new posts so far…")
            time.sleep(delay)

        if not new_posts:
            print("No new posts found since last scrape.")
            return []

        # Build output path
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"reddit_new_until_seen_{timestamp}.csv"
        elif not filename.endswith(".csv"):
            filename = f"reddit_{filename}.csv"
        else:
            filename = f"reddit_{filename}"
        out_dir = "./datasets"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, filename)

        # Fetch comments and periodically flush to CSV
        print(f"\nFetching comments for {len(new_posts)} new posts…")
        rows: List[Dict] = []
        for i, post in enumerate(new_posts, 1):
            print(f"  [{i}/{len(new_posts)}] {post['title'][:60]}…")
            post["comments"] = self.get_post_comments(
                subreddit, post["id"], limit=comments_per_post
            )
            rows.append(self._post_to_row(post, subreddit))
            time.sleep(delay)

            if i % save_every == 0:
                print(f"  Checkpoint saved ({i} posts so far)…")
                self._save_csv(rows, out_path)

        self._save_csv(rows, out_path)
        print(f"Saved {len(rows)} posts -> {out_path}")

        # Update marker
        newest_id = new_posts[0]["id"]
        self._save_last_seen_id(subreddit, newest_id)
        print(f"Marker updated: last_seen_id = {newest_id}")

        return new_posts

    def scrape_subreddit(
        self,
        subreddit: str,
        post_limit: int = 25,
        comments_per_post: int = 50,
        sort: str = "hot",
    ) -> List[Dict]:
        print(f"Scraping r/{subreddit} for {post_limit} posts sorted by {sort}...")
        posts = self.get_subreddit_posts(subreddit, limit=post_limit, sort=sort)
        if not posts:
            print("No posts found.")
            return []

        print(f"Found {len(posts)} posts. Fetching comments...")
        for i, post in enumerate(posts, 1):
            print(f"  Processing post {i}/{len(posts)}: {post['title'][:50]}...")
            post["comments"] = self.get_post_comments(
                subreddit, post["id"], limit=comments_per_post
            )
            time.sleep(1)

        print(f"Completed scraping r/{subreddit}")
        return posts

    def scrape_and_save(
        self,
        subreddit: str,
        post_limit: int = 25,
        comments_per_post: int = 50,
        sort: str = "hot",
        filename: Optional[str] = None,
    ) -> str:
        """Scrape a subreddit by sort order and save to CSV."""
        data = self.scrape_subreddit(subreddit, post_limit, comments_per_post, sort)
        if not data:
            print("No data to save.")
            return ""

        if filename is None:
            timestamp = datetime.now().strftime("%Y_%m_%d")
            filename = f"reddit_{subreddit}_{timestamp}.csv"
        elif not filename.endswith(".csv"):
            filename = f"reddit_{filename}.csv"
        else:
            filename = f"reddit_{filename}"

        out_dir = "./datasets"
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, filename)

        rows = [self._post_to_row(post, subreddit) for post in data]
        self._save_csv(rows, out_path)
        print(f"Saved {len(rows)} posts -> {out_path}")
        return out_path