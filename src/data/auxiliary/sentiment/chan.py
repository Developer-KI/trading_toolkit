import os
import re
import shutil
import time
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


class ChanBoardScraper:
    def __init__(
        self,
        board_name: str = "biz",
        archive: bool = True,
        directory: str = "datasets",
        file_name: str = "4chan_extracted_links.csv",
    ):
        self.board_name = board_name
        self.archive = archive
        self.base_url = "https://boards.4chan.org"

        if archive:
            board_path = f"/{board_name}/archive"
        else:
            board_path = f"/{board_name}"

        self.board_url = urljoin(self.base_url, board_path)
        self.output_dir = os.path.join(directory, board_name)
        self.csv_file_path = os.path.join(self.output_dir, file_name)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            }
        )

    def scrape_board(self) -> str:
        """
        Scrape thread links from the board or its archive.

        Returns:
            Path to the saved CSV file.

        Raises:
            requests.RequestException: If the HTTP request fails.
            ValueError: If no threads are found on the page.
        """
        print(f"Fetching {self.board_url} ...")

        response = self.session.get(self.board_url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        if self.archive:
            rows_list = self._parse_archive(soup)
        else:
            rows_list = self._parse_live_board(soup)

        if not rows_list:
            raise ValueError(
                f"No threads found on {self.board_url}. "
                "The page structure may have changed."
            )

        df = pd.DataFrame(rows_list, columns=["No", "Link"])

        os.makedirs(os.path.dirname(self.csv_file_path), exist_ok=True)
        df.to_csv(self.csv_file_path, index=False)

        print(f"Saved {len(df)} thread links -> {self.csv_file_path}")
        return self.csv_file_path

    # ──────────────────────────────────────────
    # PARSING: ARCHIVE PAGES
    # ──────────────────────────────────────────

    def _parse_archive(self, soup: BeautifulSoup) -> list[dict]:
        """Parse thread links from an archive page (#arc-list table)."""
        table = soup.find("table", {"id": "arc-list"})
        if not table:
            print(
                "Warning: Archive table (#arc-list) not found. "
                f"/{self.board_name}/ may not have a web archive."
            )
            return []

        rows_list = []
        for row in table.find_all("tr"):
            number_cell = row.find("td")
            if not number_cell:
                continue

            post_number = number_cell.get_text(strip=True)

            link_cell = row.find("a", {"class": "quotelink"})
            if not link_cell:
                continue

            link = urljoin(self.base_url, link_cell["href"])
            rows_list.append({"No": post_number, "Link": link})

        return rows_list

    # ──────────────────────────────────────────
    # PARSING: LIVE BOARD PAGES
    # ──────────────────────────────────────────

    def _parse_live_board(self, soup: BeautifulSoup) -> list[dict]:
        """Parse thread links from a live board page."""
        rows_list = []

        threads = soup.find_all("div", class_="thread")

        for thread in threads:
            thread_id = thread.get("id", "")
            if thread_id.startswith("t"):
                post_number = thread_id[1:]
            else:
                post_info = thread.find("span", class_="postNum")
                if not post_info:
                    continue
                number_link = post_info.find("a", title="Reply to this post")
                if not number_link:
                    continue
                post_number = number_link.get_text(strip=True)

            link = f"{self.base_url}/{self.board_name}/thread/{post_number}"
            rows_list.append({"No": post_number, "Link": link})

        return rows_list


class PostScraper:
    def __init__(
        self,
        csv_path: str,
        output_dir: str | None = "datasets",
        output_file: str = "4chan_post_replies_dataset.csv",
        delay: float = 1.0,
        save_every: int = 10,
        cleanup: bool = True,
    ):
        """
        Args:
            csv_path:    Path to the CSV of thread links (from ChanBoardScraper).
            output_dir:  Output directory for the dataset CSV.
                         Defaults to the same directory as csv_path.
            output_file: Output CSV filename.
            delay:       Seconds to wait between HTTP requests.
            save_every:  Save to disk every N threads (batch saving).
            cleanup:     If True, delete the links directory after scraping.
        """
        self.csv_path = csv_path
        self.links_dir = os.path.dirname(csv_path) or "datasets"
        self.output_dir = output_dir or self.links_dir
        self.output_path = os.path.join(self.output_dir, output_file)
        self.delay = delay
        self.save_every = save_every
        self.cleanup = cleanup

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            }
        )

        os.makedirs(self.output_dir, exist_ok=True)
        self.dataset: list[dict] = self._load_dataset()

    def _load_dataset(self) -> list[dict]:
        """Load existing dataset from disk, or return an empty list."""
        if os.path.isfile(self.output_path):
            try:
                df = pd.read_csv(self.output_path)
                print(f"Loaded existing dataset from {self.output_path}")
                return df.to_dict(orient="records")
            except Exception as e:
                print(f"Could not read {self.output_path} ({e}), starting fresh.")
        return []

    def _save_dataset(self):
        """Write the current dataset to disk as CSV."""
        df = pd.DataFrame(self.dataset)
        df.to_csv(self.output_path, index=False)

    def load_links(self) -> list[str]:
        """Read thread links from the CSV file."""
        try:
            df = pd.read_csv(self.csv_path)
            return df["Link"].tolist()
        except Exception as e:
            print(f"Error reading {self.csv_path}: {e}")
            return []

    def scrape_posts(self):
        """
        Scrape all threads listed in the CSV.

        Visits each thread URL, extracts the original post and replies,
        classifies the post as a question or not, and appends it to the
        dataset. Saves to disk every `save_every` threads.

        When finished, deletes the links directory if `cleanup` is True.
        """
        links = self.load_links()
        if not links:
            print("No links to scrape.")
            return

        total = len(links)
        scraped = 0
        errors = 0

        print(f"Starting to scrape {total} threads...")

        for index, link in enumerate(links, start=1):
            print(f"[{index}/{total}] {link}")

            try:
                response = self.session.get(link, timeout=30)

                if response.status_code == 404:
                    print("  Thread not found (404), skipping.")
                    errors += 1
                    continue

                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")

                main_post = self._scrape_post_content(soup)
                if not main_post:
                    print("  No post content found, skipping.")
                    errors += 1
                    continue

                replies = self._scrape_replies(soup)
                category = "question" if self._is_question(main_post) else "none_question"

                self.dataset.append(
                    {
                        "post": main_post,
                        "replies": " ||| ".join(replies),
                        "reply_count": len(replies),
                        "category": category,
                        "url": link,
                    }
                )
                scraped += 1

                if scraped % self.save_every == 0:
                    self._save_dataset()
                    print(f"  Checkpoint saved ({scraped} threads so far).")

            except requests.RequestException as e:
                print(f"  Request failed: {e}")
                errors += 1
            except Exception as e:
                print(f"  Unexpected error: {e}")
                errors += 1

            if index < total:
                time.sleep(self.delay)

        # Final save
        self._save_dataset()

        q = sum(1 for r in self.dataset if r["category"] == "question")
        nq = sum(1 for r in self.dataset if r["category"] == "none_question")
        print(
            f"\nDone. Scraped {scraped} threads, {errors} errors.\n"
            f"Dataset: {q} questions, {nq} non-questions.\n"
            f"Saved -> {self.output_path}"
        )

        # Cleanup the links directory
        if self.cleanup:
            self._cleanup_links_dir()

    def _cleanup_links_dir(self):
        """Delete the directory that held the thread-links CSV."""
        if os.path.isdir(self.links_dir):
            shutil.rmtree(self.links_dir)
            print(f"Cleaned up links directory: {self.links_dir}")

    def _scrape_post_content(self, soup: BeautifulSoup) -> str:
        """Extract the original post text from the thread."""
        post_el = soup.find("blockquote", class_="postMessage")
        if not post_el:
            return ""
        return post_el.get_text(strip=True)

    def _scrape_replies(self, soup: BeautifulSoup) -> list[str]:
        """Extract all reply texts from the thread."""
        replies = []
        for reply in soup.find_all("div", class_="post reply"):
            content = reply.find("blockquote", class_="postMessage")
            if content:
                text = content.get_text(strip=True)
                if text:
                    replies.append(text)
        return replies

    @staticmethod
    def _is_question(text: str) -> bool:
        """Check if a post is a question (ends with ?)."""
        return bool(re.search(r"\?\s*$", text))
