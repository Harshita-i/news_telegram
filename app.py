import os
import requests
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.helpers import escape_markdown
from dotenv import load_dotenv
import threading
import sqlite3
import re
from flask import Flask

DB_PATH = os.path.join(os.path.dirname(__file__), "memory.db")

# --- ENVIRONMENT AND KEYS ---
load_dotenv()
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- SQLITE MEMORY INITIALIZE ---
def init_db():
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    # Create table if not exists
    c.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            input_topic TEXT,
            news_data TEXT,
            summary TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def init_alerts_db():
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            keyword TEXT
        )
    """)
    conn.commit()
    conn.close()



async def notify_alerts(context, news_data):
    # conn = sqlite3.connect("memory.db")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT chat_id, keyword FROM alerts")
    rows = c.fetchall()
    conn.close()
    already_notified = set()
    for chat_id, keyword in rows:
        # Improved check: whole word, case-insensitive
        pattern = r'\b{}\b'.format(re.escape(keyword))
        if re.search(pattern, news_data, flags=re.IGNORECASE) and chat_id not in already_notified:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=f"🔔 ALERT: The keyword '{keyword}' appears in the latest news!"
            )
            already_notified.add(chat_id)




def log_interaction(chat_id, input_topic, news_data, summary):
    print("Logging chat_id:", chat_id)
    # conn = sqlite3.connect("memory.db")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO interactions (chat_id, input_topic, news_data, summary) VALUES (?, ?, ?, ?)",
        (str(chat_id), input_topic, news_data, summary)
    )
    conn.commit()
    conn.close()
    print("✅ Interaction logged successfully")

# Initialize database
init_db()
init_alerts_db()

# --- GEMINI CONFIGURATION ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"Gemini config failed: {e}")

# --- MAIN TOOLS ---
CATEGORIES = ["top", "business", "politics", "sports", "world", "health", "science", "technology"]

async def get_news(topic_or_category: str) -> str:
    if not GNEWS_API_KEY:
        return "Error: GNews API Key not found."
    if topic_or_category in CATEGORIES:
        url = f"https://gnews.io/api/v4/top-headlines?category={topic_or_category}&lang=en&country=in&max=5&apikey={GNEWS_API_KEY}"
    else:
        url = f"https://gnews.io/api/v4/search?q={topic_or_category}&lang=en&country=in&max=5&apikey={GNEWS_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 403:
            return "Error: Invalid or expired GNews API key (403 Forbidden)."
        elif response.status_code == 429:
            return "Error: GNews Request limit reached (429 Too Many Requests)."
        elif response.status_code != 200:
            return f"Error: Unexpected response from GNews (status {response.status_code})."
        data = response.json()
        if "articles" not in data or not data["articles"]:
            return f"No news found for '{topic_or_category}' at the moment."
        articles = data["articles"]
        titles = [f"{a['title']} - {a.get('description', 'No description available.')}" for a in articles]
        return "\n".join(titles)
    except requests.exceptions.RequestException as e:
        return f"Error: Network issue occurred while contacting GNews: {e}"

async def summarize_with_ai(text: str) -> str:
    if not GEMINI_API_KEY:
        return "AI summarization failed: Gemini API Key missing."
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        system_prompt = (
            "You are an expert news analyst. Summarize the following news headlines and snippets "
            "into exactly 5 short, clear, and objective bullet points. Use only the provided context."
        )
        response = model.generate_content(
            contents=[system_prompt, f"News Data:\n{text}"]
        )
        return response.text.strip()
    except Exception as e:
        return f"An unexpected error occurred during summarization: {e}"

# --- TELEGRAM HANDLER ---
async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    input_topic = " ".join(context.args).lower().strip() or "India"
    display_topic = "Top Headlines" if input_topic == "top" else input_topic.capitalize()
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 Searching GNews and summarizing news for *{display_topic}*...",
        parse_mode="Markdown"
    )
    news_data = await get_news(input_topic)
    await notify_alerts(context, news_data)
    if news_data.startswith(("Error:", "No news")):
        await context.bot.send_message(chat_id=chat_id, text=news_data)
        return
    summary = await summarize_with_ai(news_data)
    if summary.startswith(("AI summarization failed:", "Gemini AI API error:")):
        await context.bot.send_message(chat_id=chat_id, text=summary)
        return
    summary_safe = escape_markdown(summary, version=2)
    display_safe = escape_markdown(display_topic, version=2)
    final_digest = f"AI News Digest: *{display_safe}*\n\n{summary_safe}"
    if len(final_digest) > 4000:
        final_digest = final_digest[:3990] + "..."
    await context.bot.send_message(
        chat_id=chat_id,
        text=final_digest,
        parse_mode="MarkdownV2"
    )
    if CHANNEL_ID and str(CHANNEL_ID).startswith("-100"):
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=final_digest,
                parse_mode="MarkdownV2"
            )
        except Exception as e:
            print(f"Failed to broadcast to channel {CHANNEL_ID}: {e}")
    # Save user request and response to SQLite (true persistent memory)
    log_interaction(chat_id, input_topic, news_data, summary)

from telegram.constants import ParseMode

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    print("Current chat_id:", chat_id)
    # Default: Show last 3 entries; allow user to specify /history 5 etc.
    args = context.args
    try:
        n = int(args[0]) if args and args[0].isdigit() else 3
    except Exception:
        n = 3

    # Fetch last n interactions for this user
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, input_topic, summary
        FROM interactions
        WHERE chat_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """, (str(chat_id), n))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="No history found yet. Try /news <topic>!")
        return

    # Format history nicely  
    msg_lines = ["Your last {} news requests:".format(n)]
    for i, (timestamp, topic, summary) in enumerate(rows, 1):
        msg_lines.append(f"{i}. [{timestamp}] Topic: {topic}\n   Summary: {summary[:120]}...")  # Truncate long summaries

    response = "\n\n".join(msg_lines)
    await context.bot.send_message(chat_id=chat_id, text=response, parse_mode=ParseMode.MARKDOWN)

async def mytopics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    # Count frequency of each topic requested by the user
    c.execute("""
        SELECT input_topic, COUNT(*) as count
        FROM interactions
        WHERE chat_id = ?
        GROUP BY input_topic
        ORDER BY count DESC
        LIMIT 5
    """, (str(chat_id),))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="No topics found yet! Try /news <topic> to start building your news profile.")
        return

    # Format the output
    msg_lines = ["Your top requested news topics:"]
    for i, (topic, count) in enumerate(rows, 1):
        msg_lines.append(f"{i}. {topic} ({count} times)")
    response = "\n".join(msg_lines)
    await context.bot.send_message(chat_id=chat_id, text=response)

async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    # Get top trending topics (by all users, top 10)
    c.execute("""
        SELECT input_topic
        FROM interactions
        GROUP BY input_topic
        ORDER BY COUNT(*) DESC
        LIMIT 10
    """)
    trending = set(row[0] for row in c.fetchall())

    # Get topics explored by this user
    c.execute("""
        SELECT DISTINCT input_topic
        FROM interactions
        WHERE chat_id = ?
    """, (str(chat_id),))
    personal = set(row[0] for row in c.fetchall())
    conn.close()

    # Find trending topics not yet explored by user
    unexplored = list(trending - personal)

    if not unexplored:
        msg = "🎉 You've already explored all top trending topics! Try /news to find more."
    else:
        msg_lines = ["🔥 Trending topics you haven't explored yet:"]
        for i, topic in enumerate(unexplored, 1):
            msg_lines.append(f"{i}. {topic}")
        msg_lines.append("Try /news <topic> to read about these!")
        msg = "\n".join(msg_lines)
    await context.bot.send_message(chat_id=chat_id, text=msg)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await context.bot.send_message(chat_id=chat_id, text="Please provide a keyword to subscribe to alerts (e.g., /alert AI)")
        return
    keyword = " ".join(context.args).strip().lower()
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("INSERT INTO alerts (chat_id, keyword) VALUES (?, ?)", (str(chat_id), keyword))
    conn.commit()
    conn.close()
    await context.bot.send_message(chat_id=chat_id, text=f"Alert set! You'll be notified when '{keyword}' appears in future news.")

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("SELECT keyword FROM alerts WHERE chat_id = ?", (str(chat_id),))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await context.bot.send_message(chat_id=chat_id, text="You don't have any active alerts. Set one using /alert <keyword>!")
        return
    msg_lines = ["Your active alert keywords:"]
    for i, (keyword,) in enumerate(rows, 1):
        msg_lines.append(f"{i}. {keyword}")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(msg_lines))

async def removealert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await context.bot.send_message(chat_id=chat_id, text="Specify the keyword to remove: /removealert <keyword>")
        return
    keyword = " ".join(context.args).strip().lower()
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE chat_id = ? AND keyword = ?", (str(chat_id), keyword))
    removed = c.rowcount
    conn.commit()
    conn.close()
    if removed:
        await context.bot.send_message(chat_id=chat_id, text=f"Removed alert for '{keyword}'.")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"No alert found for '{keyword}'.")

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect("memory.db")
    c = conn.cursor()
    c.execute("""
        SELECT input_topic, COUNT(*) as count
        FROM interactions
        GROUP BY input_topic
        ORDER BY count DESC
        LIMIT 5
    """)
    rows = c.fetchall()
    conn.close()
    if not rows:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="No trending topics yet! Be the first to use /news <topic>.")
        return
    msg_lines = ["Top trending news topics (all users):"]
    for i, (topic, count) in enumerate(rows, 1):
        msg_lines.append(f"{i}. {topic} ({count} requests)")
    await context.bot.send_message(chat_id=update.effective_chat.id, text="\n".join(msg_lines))






# Create a dummy Flask app
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Telegram Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)


# --- MAIN ---
def main():
    if not BOT_TOKEN:
        print("\nFATAL ERROR: TELEGRAM_BOT_TOKEN is missing. Please set the environment variable.")
    else:
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("news", news_command))
        app.add_handler(CommandHandler("history", history_command))
        app.add_handler(CommandHandler("mytopics", mytopics_command))
        app.add_handler(CommandHandler("alert", alert_command))
        app.add_handler(CommandHandler("alerts", alerts_command))
        app.add_handler(CommandHandler("removealert", removealert_command))
        app.add_handler(CommandHandler("trending", trending_command))
        app.add_handler(CommandHandler("discover", discover_command))

        print("\n" + "="*50)
        print("Bot is initialized and ready to poll!")
        print("Type: /news technology OR /news sports")
        print("="*50 + "\n")
        app.run_polling()
        



if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    main()

