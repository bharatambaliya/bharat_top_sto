import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from googletrans import Translator
import telegram
import html2text
import base64
import re
import os
import time

# Connect to MongoDB
mongo_url = os.getenv('MONGO_URL')  # Use 'MONGO_URL' or whatever you named the secret
client = MongoClient(mongo_url)
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

# Initialize translator
translator = Translator()

# Promotional message
promo_message = os.getenv('promo_message')

def get_wp_token():
    credentials = wp_user + ':' + wp_pass
    token = base64.b64encode(credentials.encode())
    return {'Authorization': 'Basic ' + token.decode('utf-8')}

def create_wp_post(title, content, excerpt):
    headers = get_wp_token()
    headers['Content-Type'] = 'application/json'
    data = {
        'title': title,
        'content': content,
        'excerpt': excerpt,
        'status': 'publish',
        'categories': [4]  # Stock News category ID
    }
    response = requests.post(wp_url + '/posts', headers=headers, json=data)
    if response.status_code == 201:
        return response.json()['link'], response.json()['id']
    else:
        print(f"Failed to create WordPress post: {response.text}")
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
    """Translate text with retry mechanism. Fallback to original if failed."""
    attempt = 0
    while attempt < retries:
        try:
            return translator.translate(text, dest=dest_lang).text
        except Exception as e:
            print(f"Translation failed: {e}. Retrying ({attempt + 1}/{retries})...")
            time.sleep(2)  # Wait before retrying
            attempt += 1
    print("Translation failed after multiple attempts. Returning original text.")
    return text

def truncate_text(text, limit=4990):
    """Truncate text to a specific character limit."""
    return text[:limit] + '...' if len(text) > limit else text

def scrape_and_process_url(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')
    
    content_classes = ["storyPage_storyBox__zPlkE", "storyPage_storyContent__m_MYl", "your_other_class_name"]
    content_div = None
    for class_name in content_classes:
        content_div = soup.find('div', class_=class_name)
        if content_div:
            break
    
    if not content_div:
        print(f"Main content div not found for URL: {url}")
        return None
    
    title = content_div.find('h1', id="article-0")
    if not title:
        print(f"Title not found for URL: {url}")
        return None
    title_text = title.get_text(strip=True)
    
    summary = content_div.find('h2', class_="storyPage_summary__Ge5SX")
    summary_text = summary.get_text(strip=True) if summary else ""
    
    content = []
    for div in content_div.find_all('div', id=lambda x: x and x.startswith('article-index-')):
        for element in div.descendants:
            if element.name in ['h2', 'h3', 'h4', 'p']:
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
    
    summary_gujarati = translate_text(summary_text)
    content_gujarati = translate_text(cleaned_content_html)
    
    full_content = f"{summary_gujarati}\n\n{content_gujarati}"
    
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
        
        bot.send_message(chat_id=telegram_channel_id, text=telegram_message, parse_mode=telegram.ParseMode.HTML)
        
        return {
            'title': title_text,
            'content': cleaned_content_html,
            'url': url,
            'wp_post_url': post_url,
            'wp_post_id': post_id
        }
    else:
        print(f"Failed to process URL: {url}")
        return None

# URL to scrape
base_url = "https://www.livemint.com/"
scrape_url = "https://www.livemint.com/market/stock-market-news"

response = requests.get(scrape_url)
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
                    article_data = scrape_and_process_url(target_url)
                    if article_data:
                        collection.insert_one(article_data)
                        print(f"Processed and inserted new URL: {target_url}")
                else:
                    print(f"URL already processed: {target_url}")

print("Finished scraping and processing new articles.")
