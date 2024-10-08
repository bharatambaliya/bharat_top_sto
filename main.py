import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from deep_translator import GoogleTranslator
import telegram
from telegram.constants import ParseMode
import html2text
import base64
import re
import os
import time
import logging
import asyncio
from requests.exceptions import RequestException

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Connect to MongoDB
client = MongoClient(os.getenv('client'))
db = client['stock_news']
collection = db['urls']

# WordPress configuration
wp_url = os.getenv('wp_url')
wp_user = os.getenv('wp_user')
wp_pass = os.getenv('wp_pass')

# Telegram configuration
telegram_bot_token = os.getenv('telegram_bot_token')
telegram_channel_id = os.getenv('telegram_channel_id')
bot = telegram.Bot(token=telegram_bot_token)

# Promotional message
promo_message = os.getenv('promo_message')


def get_wp_token():
    credentials = f"{wp_user}:{wp_pass}"
    token = base64.b64encode(credentials.encode())
    return {'Authorization': f'Basic {token.decode("utf-8")}'}


async def send_telegram_message(chat_id, text):
    """Send a message using the Telegram bot."""
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


def create_wp_post(title, content, excerpt, max_retries=3, delay=5):
    headers = get_wp_token()
    headers['Content-Type'] = 'application/json'
    data = {
        'title': title,
        'content': content,
        'excerpt': excerpt,
        'status': 'publish',
        'categories': [4]  # Stock News category ID
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(f"{wp_url}/posts", headers=headers, json=data, timeout=30)
            response.raise_for_status()

            if response.status_code == 201:
                return response.json()['link'], response.json()['id']
            else:
                logging.warning(f"Unexpected status code: {response.status_code}")
                logging.warning(f"Response content: {response.text[:500]}...")

        except RequestException as e:
            logging.error(f"Request failed on attempt {attempt + 1}: {str(e)}")

        if attempt < max_retries - 1:
            time.sleep(delay * (2 ** attempt))  # Exponential backoff

    logging.error(f"Failed to create WordPress post after {max_retries} attempts.")
    return None, None


def clean_html_content(content):
    """Remove empty tags and filter out duplicate content."""
    cleaned_content = re.sub(r'<(?!br)(\w+)([^>]*)>\s*</\1>', '', content)
    lines = cleaned_content.split('\n')
    seen_lines = set()
    filtered_content = []
    for line in lines:
        normalized_line = re.sub(r'\s+', ' ', line.strip().lower())
        if normalized_line not in seen_lines and normalized_line:
            filtered_content.append(line)
            seen_lines.add(normalized_line)

    return '\n'.join(filtered_content)


def translate_with_method_1(text, dest_lang='gu'):
    """Primary translation method using GoogleTranslator from deep_translator."""
    return GoogleTranslator(source='auto', target=dest_lang).translate(text)


def translate_with_method_2(text, dest_lang='gu'):
    """Secondary translation method (e.g., another API). Placeholder for now."""
    # Placeholder for secondary translation service
    # In real usage, replace this with another translation API or service
    return GoogleTranslator(source='auto', target=dest_lang).translate(text)


def translate_text_with_two_methods(text, dest_lang='gu'):
    """Try two translation methods. Return original text if both fail."""
    if not text:
        logging.error("Text is None or empty, skipping translation.")
        return text

    # Try the first translation method
    try:
        translated_text = translate_with_method_1(text, dest_lang)
        if translated_text:
            return translated_text
    except Exception as e:
        logging.warning(f"Primary translation method failed: {e}")

    # Try the second translation method
    try:
        translated_text = translate_with_method_2(text, dest_lang)
        if translated_text:
            return translated_text
    except Exception as e:
        logging.warning(f"Secondary translation method failed: {e}")

    # If both methods fail, return the original text
    logging.error("Both translation methods failed. Returning original text.")
    return text


def truncate_text(text, limit=500):
    """Truncate text to a specific character limit."""
    return text[:limit] + '...' if len(text) > limit else text


async def scrape_and_process_url(url):
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        content_classes = ["storyPage_storyBox__zPlkE", "storyPage_storyContent__m_MYl", "your_other_class_name"]
        content_div = None
        for class_name in content_classes:
            content_div = soup.find('div', class_=class_name)
            if content_div:
                break

        if not content_div:
            logging.warning(f"Main content div not found for URL: {url}")
            return None

        title = content_div.find('h1', id="article-0")
        if not title:
            logging.warning(f"Title not found for URL: {url}")
            return None
        title_text = title.get_text(strip=True)

        summary = content_div.find('h2', class_="storyPage_summary__Ge5SX")
        summary_text = summary.get_text(strip=True) if summary else ""

        content = []
        for div in content_div.find_all('div', id=lambda x: x and x.startswith('article-index-')):
            for element in div.descendants:
                if element.name:
                    manualbacklink = element.find('a', class_='manualbacklink')
                    if manualbacklink:
                        bold_parent = manualbacklink.find_parent('b')
                        if bold_parent:
                            content.append(f"<b>{manualbacklink.get_text(strip=True)}</b>")
                        else:
                            content.append(manualbacklink.get_text(strip=True))
                    else:
                        for link in element.find_all('a'):
                            link.unwrap()
                        content.append(str(element))

        content_html = ''.join(content)
        cleaned_content_html = clean_html_content(content_html)

        # Use the two translation methods and fallback to original if both fail
        summary_gujarati = translate_text_with_two_methods(summary_text)
        content_gujarati = translate_text_with_two_methods(cleaned_content_html)

        # Check if translation failed (i.e., original content was returned)
        if summary_gujarati == summary_text and content_gujarati == cleaned_content_html:
            # If translation fails, only include original content without duplication
            full_content = f"{cleaned_content_html}"
        else:
            # Otherwise, include both translated and original content for readability
            full_content = f"{summary_gujarati}\n\n{content_gujarati}\n\n<h2>Original Content:</h2>\n{cleaned_content_html}"

        # Create WordPress post
        post_url, post_id = create_wp_post(title_text, full_content, summary_gujarati)

        if post_url and post_id:
            h = html2text.HTML2Text()
            h.ignore_links = True
            plain_content = h.handle(cleaned_content_html)
            truncated_content = truncate_text(plain_content)

            # Send English summary in Telegram instead of Gujarati content
            summary_english = truncate_text(summary_text)

            telegram_message = (
                f"🔷 <b>{title_text}</b>\n\n"
                f"📄 <i>{summary_english}</i>\n\n"
                f"📌 <b>સંપુર્ણ આર્તિકલ અંગ્રેજી અને ગુજરાતી બન્ને ભાષામાં વાંચવા માટે અહીં ક્લિક કરો:</b> <a href='{post_url}'>🖱️ {post_url}</a>\n\n"
                f"💼 {promo_message}\n\n"
                f"🔹 Follow us for more updates!\n"
                f"🔹 Stay informed with the latest stock news!"
                f"🔹 Join Our Telegram Channel :- @DalalStreetGujarati "
            )

            # Send Telegram message
            await send_telegram_message(chat_id=telegram_channel_id, text=telegram_message)

            # Return data to insert into MongoDB
            return {
                'title': title_text,
                'content': cleaned_content_html,
                'url': url,
                'wp_post_url': post_url,
                'wp_post_id': post_id
            }
        else:
            logging.error(f"Failed to process URL: {url}")
            return None
    except Exception as e:
        logging.error(f"Error processing URL {url}: {str(e)}")
        return None


async def main():
    base_url = "https://www.livemint.com/"
    scrape_url = "https://www.livemint.com/market/stock-market-news"

    try:
        response = requests.get(scrape_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        main_section = soup.find('section', class_='mainSec')
        if main_section:
            listview_div = main_section.find('div', id='listview')
            if listview_div:
                links = listview_div.find_all('a')
                for link in links:
                    onclick_attr = link.get('onclick')
                    if onclick_attr and "target_url" in onclick_attr:
                        start = onclick_attr.find("target_url: '") + len("target_url: '")
                        end = onclick_attr.find("'", start)
                        target_url = onclick_attr[start:end]

                        if target_url.startswith('/'):
                            target_url = base_url + target_url.lstrip('/')

                        if not collection.find_one({'url': target_url}):
                            article_data = await scrape_and_process_url(target_url)
                            if article_data:
                                collection.insert_one(article_data)
                                logging.info(f"Processed and inserted new URL: {target_url}")
                        else:
                            logging.info(f"URL already processed: {target_url}")
            else:
                logging.warning("Listview div not found in the main section.")
        else:
            logging.warning("Main section not found in the page.")
    except Exception as e:
        logging.error(f"Error scraping URL {scrape_url}: {str(e)}")


if __name__ == '__main__':
    asyncio.run(main())
