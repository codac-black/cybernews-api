import os
import json
import logging
import re
import requests
import time
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
from datetime import datetime, timedelta
from typing import List, Optional
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
# Import RotatingFileHandler directly
from logging.handlers import RotatingFileHandler
import csv


# Define the Article dataclass including a new field for the summary.
@dataclass
class Article:
    title: str
    link: str
    category: Optional[str] = None
    author: Optional[str] = None
    published_date: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None  # New field for the article summary


class CyberNewsFeed:
    def __init__(self, config_path: str = "config.json"):
        # Load environment variables
        self._load_environment()
        # Setup logging with rotation
        self._setup_logging()
        # Initialize HTTP session with retry strategy
        self.session = self._create_session()
        # Load configuration (or use defaults)
        self.config = self._load_config(config_path)
        # Initialize storage for duplicate checking
        self.storage_path = Path("data")
        self.storage_path.mkdir(exist_ok=True)

    def _load_environment(self) -> None:
        """Load environment variables with validation."""
        load_dotenv()
        self.discord_webhook_url = os.getenv('DISCORD_WEBHOOK_URL')
        if not self.discord_webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL environment variable is required")

    def _setup_logging(self) -> None:
        """Configure rotating file handler with proper formatting."""
        log_file = Path("logs/cyber_feed.log")
        log_file.parent.mkdir(exist_ok=True)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=1024 * 1024,  # 1MB
            backupCount=3
        )
        file_handler.setFormatter(formatter)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        # Remove any existing handlers
        self.logger.handlers = []
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry strategy and browser headers."""
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session = requests.Session()
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        # Add headers to mimic a browser
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0'
        })
        return session

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from a JSON file or use default values."""
        default_config = {
            "sources": [{
                "name": "BleepingComputer",
                "url": "https://www.bleepingcomputer.com/news/security/",
                "article_selector": "li:has(div.bc_latest_news_text)",
                "title_selector": "h4 a",
                "category_selector": "div.bc_latest_news_category a",
                "description_selector": "p",
                "author_selector": "li.bc_news_author a",
                "date_selector": "li.bc_news_date",
                "exclude_sponsored": True
            }],
            "max_articles": 5,
            "check_interval": 300  # 5 minutes
        }
        try:
            with open(config_path) as f:
                return {**default_config, **json.load(f)}
        except FileNotFoundError:
            self.logger.warning(f"Config file {config_path} not found, using defaults")
            return default_config

    def get_articles(self, source: dict) -> List[Article]:
        """
        Fetch and parse articles from a source, then generate a summary for each article.
        """
        try:
            response = self.session.get(source["url"], timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            article_elements = soup.select(source["article_selector"])
            articles = []
            for element in article_elements[:self.config["max_articles"]]:
                # Skip sponsored content if configured
                if source.get("exclude_sponsored", True) and "Sponsored Content" in element.text:
                    continue
                title_element = element.select_one(source["title_selector"])
                if not title_element:
                    continue
                title = title_element.text.strip()
                link = title_element.get('href', '')
                if not link.startswith('http'):
                    link = source.get('link_prefix', '') + link
                # Create the article object without the summary yet.
                article = Article(
                    title=title,
                    link=link,
                    category=self._get_text(element, source["category_selector"]),
                    description=self._get_text(element, source["description_selector"]),
                    author=self._get_text(element, source["author_selector"]),
                    published_date=self._get_text(element, source["date_selector"])
                )
                # Generate a summary by opening the article link and extracting text.
                article.summary = self.generate_summary(link)
                articles.append(article)
            self.logger.info(f"Successfully fetched {len(articles)} articles from {source['name']}")
            return articles
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching articles from {source['name']}: {str(e)}")
            return []

    def _get_text(self, element, selector: str) -> Optional[str]:
        """Helper method to safely extract text from a specified selector."""
        try:
            found = element.select_one(selector)
            return found.text.strip() if found else None
        except Exception:
            return None

    def generate_summary(self, url: str, sentence_count: int = 2) -> Optional[str]:
        """
        Open the given URL, extract its textual content, and return a summary composed
        of the first few sentences. This version first attempts to extract text from a
        container with the class 'articleBody'. If not found, it falls back to all <p> tags.
        """
        try:
            self.logger.info(f"Generating summary for {url}")
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # Try to select the main article content
            article_body = soup.select_one("div.articleBody")
            if article_body:
                # Get text from all paragraphs within the article body.
                paragraphs = [p.get_text().strip() for p in article_body.find_all('p')]
            else:
                # Fallback: get text from all <p> tags on the page.
                paragraphs = [p.get_text().strip() for p in soup.find_all('p')]

            full_text = " ".join(paragraphs).strip()

            if full_text:
                # Use a simple regex to split the text into sentences.
                sentences = re.split(r'(?<=[.!?])\s+', full_text)
                summary_sentences = sentences[:sentence_count]
                summary = " ".join(summary_sentences)
                # print(summary)
                return summary
            else:
                return None
        except Exception as e:
            self.logger.error(f"Error generating summary for {url}: {e}")
            return None

    def remove_duplicates(self, articles: List[Article]) -> List[Article]:
        """Remove duplicate articles using a persistent JSON store."""
        seen_file = self.storage_path / "seen_articles.json"
        try:
            if seen_file.exists():
                with seen_file.open('r') as f:
                    seen_articles = json.load(f)
            else:
                seen_articles = []
            # Remove articles older than 7 days
            current_time = datetime.now()
            seen_articles = [
                article for article in seen_articles
                if datetime.fromisoformat(article["timestamp"]) > current_time - timedelta(days=7)
            ]
            new_unique_articles = []
            for article in articles:
                if not any(seen["link"] == article.link for seen in seen_articles):
                    new_unique_articles.append(article)
                    seen_articles.append({
                        "link": article.link,
                        "timestamp": datetime.now().isoformat()
                    })
            # Save updated seen articles
            with seen_file.open('w') as f:
                json.dump(seen_articles, f, indent=2)
            return new_unique_articles
        except Exception as e:
            self.logger.error(f"Error handling duplicate removal: {str(e)}")
            return []

    def save_to_csv(self, articles: List[Article], file_path: str) -> None:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        """Save articles to a CSV file."""
        fieldnames = ['title', 'link', 'category', 'author', 'published_date', 'description', 'summary']
        with open(file_path, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for article in articles:
                writer.writerow({
                    'title': article.title,
                    'link': article.link,
                    'category': article.category,
                    'author': article.author,
                    'published_date': article.published_date,
                    'description': article.description,
                    'summary': article.summary
                })

    def send_to_discord(self, articles: List[Article]) -> None:
        """Send articles to Discord using a webhook with proper error handling."""
        if not articles:
            return
        try:
            for article in articles:
                fields = [
                    {
                        "name": "Category",
                        "value": article.category if article.category else "N/A",
                        "inline": True
                    },
                    {
                        "name": "Author",
                        "value": article.author if article.author else "N/A",
                        "inline": True
                    }
                ]
                if article.summary:
                    fields.append({
                        "name": "Summary",
                        "value": article.summary,
                        "inline": False
                    })
                message = {
                    "content": None,
                    "embeds": [{
                        "title": article.title,
                        "url": article.link,
                        "color": 5814783,
                        "description": article.description if article.description else "",
                        "fields": fields,
                        "footer": {
                            "text": f"Published: {article.published_date}" if article.published_date else "Cyber Security News Feed"
                        },
                        "timestamp": datetime.now().isoformat()
                    }]
                }
                response = self.session.post(
                    self.discord_webhook_url,
                    json=message,
                    timeout=5
                )
                response.raise_for_status()
                # Respect Discord rate limits if necessary.
                if response.status_code == 429:
                    retry_after = response.json().get('retry_after', 1)
                    time.sleep(retry_after)
        #     save to csv
            self.save_to_csv(articles, 'data/cyber_news_feed.csv')
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error sending to Discord: {str(e)}")


    def run(self) -> None:
        """Main execution loop to fetch, deduplicate, and send articles."""
        self.logger.info("Starting Cyber News Feed")
        for source in self.config["sources"]:
            try:
                articles = self.get_articles(source)
                unique_articles = self.remove_duplicates(articles)
                if unique_articles:
                    self.send_to_discord(unique_articles)
            except Exception as e:
                self.logger.error(f"Error processing source {source['name']}: {str(e)}")


if __name__ == "__main__":
    feed = CyberNewsFeed()
    feed.run()
