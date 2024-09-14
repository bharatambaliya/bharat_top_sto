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


def translate_text(text, dest_lang='gu', retries=3):
    """Translate text using Deep Translator with retry mechanism. Fallback to MyMemory if it fails."""
    if not text:
        logging.error("Text is None or empty, skipping translation.")
        return text  # Return original text if it's None or empty

    # Primary method using GoogleTranslator
    for attempt in range(retries):
        try:
            return GoogleTranslator(source='auto', target=dest_lang).translate(text)
        except Exception as e:
            logging.warning(f"Google Translation failed: {e}. Retrying ({attempt + 1}/{retries})...")
            time.sleep(2)  # Wait before retrying

    # If GoogleTranslator fails, fallback to MyMemory
    logging.warning("Primary translation method failed. Switching to MyMemory API.")
    return translate_with_mymemory(text, dest_lang)


def translate_with_mymemory(text, target_lang):
    """Fallback method for translation using MyMemory API."""
    try:
        # MyMemory API request
        response = requests.get(
            'https://api.mymemory.translated.net/get',
            params={'q': text, 'langpair': f'en|{target_lang}'},
            timeout=10
        )
        response.raise_for_status()

        json_response = response.json()
        if 'responseData' in json_response and 'translatedText' in json_response['responseData']:
            return json_response['responseData']['translatedText']
        else:
            logging.error(f"MyMemory API failed with response: {json_response}")
            return text  # Return original text if MyMemory fails
    except Exception as e:
        logging.error(f"Error with MyMemory translation: {e}")
        return text  # Return original text if MyMemory fails


def truncate_text(text, limit=500):
    """Truncate text to a specific character limit."""
    return text[:limit] + '...' if len(text) > limit else text


def style_content_paragraph_by_paragraph(english_text, gujarati_text):
    """Ensure that for each paragraph, Gujarati appears directly below the English content."""
    english_paragraphs = english_text.split("\n")
    gujarati_paragraphs = gujarati_text.split("\n")

    combined_content = ""
    for eng, guj in zip(english_paragraphs, gujarati_paragraphs):
        combined_content += f"<p>{eng}</p>\n<p style='color: #FF9933;'>{guj}</p>\n"

    return combined_content


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

        # Translations
        summary_gujarati = translate_text(summary_text)
        content_gujarati = translate_text(cleaned_content_html)

        # Combine both English and Gujarati for each paragraph
        full_content = style_content_paragraph_by_paragraph(
            english_text=cleaned_content_html,
            gujarati_text=content_gujarati
        )

        post_url, post_id = create_wp_post(title_text, full_content, summary_gujarati)

        if post_url and post_id:
            h = html2text.HTML2Text()
            h.ignore_links = True
            plain_content = h.handle(cleaned_content_html)
            truncated_content = truncate_text(plain_content)
            summary_translated = translate_text(truncated_content)

            telegram_message = (
                f"🔷 <b>{title_text}</b>\n\n"
                f"📄 <i>{summary_translated}</i>\n\n"
                f"📌 <b>વધુ વાંચવા માટે અહીં ક્લિક કરો:</b> <a href='{post_url}'>🖱️ {post_url}</a>\n\n"
                f"💼 {promo_message}\n\n"
                f"🔹 Follow us for more updates!\n"
                f"🔹 Stay informed with the latest stock news!"
            )

            await send_telegram_message(chat_id=telegram_channel_id, text=telegram_message)

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
