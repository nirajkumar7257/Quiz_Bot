import logging
import sqlite3
import asyncio
import random
import os
from telegram import Update, Poll, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, PollAnswerHandler,
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler
)

# Logging Setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ---- CONFIGURATION FROM ENV ----
TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "QuizBot")
TIMER_SECONDS = int(os.getenv("TIMER_SECONDS", "30"))

# Dynamic Database Path (Heroku, Render, VPS ke liye flexible folder setup)
DB_DIR = os.getenv("DATABASE_DIR", ".") # Agar kuch nahi diya toh same folder me banega
DB_PATH = os.path.join(DB_DIR, "quizbot_official.db")

# States for Quiz Creation
CHOOSING, QUIZ_ONGOING = range(2)
TITLE, DESC, ADDING_QUESTIONS, TIMER, SHUFFLE = range(2, 7)

# ---- DATABASE SYSTEM ----
def init_db():
    # Ensure directory exists (Agar /data/ jaise persistent storage use ho rahe ho)
    if DB_DIR != "." and not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            quiz_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, 
            creator_id INTEGER, timer INTEGER, shuffle_mode TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id INTEGER,
            question TEXT, options TEXT, correct_index INTEGER, explanation TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS rankings (
            quiz_id INTEGER, user_id INTEGER, name TEXT, score INTEGER, total INTEGER,
            PRIMARY KEY (quiz_id, user_id)
        )
    ''')
    conn.commit()
    conn.close()

def save_quiz_meta(title, desc, creator_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO quizzes (title, description, creator_id, timer, shuffle_mode) VALUES (?, ?, ?, ?, "none")', (title, desc, creator_id, TIMER_SECONDS))
    qid = cursor.lastrowid
    conn.commit()
    conn.close()
    return qid

def finalize_quiz_settings(quiz_id, timer, shuffle):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE quizzes SET timer = ?, shuffle_mode = ? WHERE quiz_id = ?', (timer, shuffle, quiz_id))
    conn.commit()
    conn.close()

def save_question(quiz_id, question, options_list, correct_idx, explanation):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    options_str = "||".join(options_list)
    cursor.execute('INSERT INTO questions (quiz_id, question, options, correct_index, explanation) VALUES (?, ?, ?, ?, ?)',
                   (quiz_id, question, options_str, correct_idx, explanation))
    conn.commit()
    conn.close()

def get_quiz_full(quiz_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT title, description, timer, shuffle_mode FROM quizzes WHERE quiz_id = ?', (quiz_id,))
    meta = cursor.fetchone()
    if not meta:
        conn.close()
        return None, []
    cursor.execute('SELECT question, options, correct_index, explanation FROM questions WHERE quiz_id = ?', (quiz_id,))
    rows = cursor.fetchall()
    conn.close()
    
    questions = []
    for r in rows:
        questions.append({"question": r[0], "options": r[1].split("||"), "correct_index": r[2], "explanation": r[3]})
    return meta, questions

def save_rank(quiz_id, user_id, name, score, total):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO rankings (quiz_id, user_id, name, score, total) VALUES (?, ?, ?, ?, ?)', (quiz_id, user_id, name, score, total))
    conn.commit()
    conn.close()

def get_leaderboard(quiz_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT name, score FROM rankings WHERE quiz_id = ? ORDER BY score DESC LIMIT 5', (quiz_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# /start & Deep Link Handling
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    args = context.args
    user_id = update.effective_user.id
    
    if args and args.startswith("quiz_"):
        quiz_id = int(args.split("_")[1])
        meta, questions = get_quiz_full(quiz_id)
        if not meta:
            await update.message.reply_text("❌ Quiz nahi mila.")
            return CHOOSING
            
        title, desc, timer, shuffle = meta
        indices = list(range(len(questions)))
        if shuffle in ['all', 'questions']:
            random.shuffle(indices)
            
        context.user_data.update({
            "play_qid": quiz_id, "questions": questions, "order": indices,
            "current_idx": 0, "score": 0, "timer": timer, "shuffle": shuffle, "active_poll_id": None
        })
        
        keyboard = [[InlineKeyboardButton("🏁 Start Quiz", callback_data=f"startplay_{quiz_id}")]]
        await update.message.reply_text(
            f"🎯 *Quiz:* {title}\n📝 *Description:* {desc}\n⏰ *Timer:* {timer}s per question\n📊 *Total Questions:* {len(questions)} Qs",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
        return CHOOSING

    await update.message.reply_text("👋 Welcome to QuizBot Clone!\n\nNaya quiz banane ke liye `/newquiz` use karein.")
    return CHOOSING

# ---- CREATION LOGIC ----
async def new_quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Chaliye naya quiz banate hain. Sabse pehle apne quiz ka *Title* likh kar bhejein:")
    return TITLE

async def process_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["c_title"] = update.message.text
    await update.message.reply_text("Acha! Ab quiz ka *Description* bhejein, ya skip karne ke liye `/skip` type karein.")
    return DESC

async def process_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    desc = "" if update.message.text == '/skip' else update.message.text
    context.user_data["c_desc"] = desc
    
    qid = save_quiz_meta(context.user_data["c_title"], desc, update.effective_user.id)
    context.user_data["c_qid"] = qid
    context.user_data["c_qcount"] = 0
    
    await update.message.reply_text(
        "Ab standard Telegram Poll feature ka use karke sawaal bhejein.\n\n"
        "👉 Chat me 📎 Attachment -> Choose **Poll** -> Turn on **Quiz Mode** -> Sawaal aur options bhar kar bhein.\n\n"
        "Sawaal bhejte rahein, aur jab saare pure ho jayein toh chat me **/done** type karein."
    )
    return ADDING_QUESTIONS

async def process_native_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poll = update.message.poll
    if poll.type != Poll.QUIZ:
        await update.message.reply_text("⚠️ Kripya setting me 'Quiz Mode' select karke hi poll bhejein!")
        return ADDING_QUESTIONS
        
    options = [o.text for o in poll.options]
    save_question(
        quiz_id=context.user_data["c_qid"], question=poll.question, options_list=options,
        correct_idx=poll.correct_option_id, explanation=poll.explanation
    )
    context.user_data["c_qcount"] += 1
    await update.message.reply_text(f"✅ Sawaal {context.user_data['c_qcount']} add ho gaya! Agla poll bhejein ya `/done` likhein.")
    return ADDING_QUESTIONS

async def done_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.user_data.get("c_qcount", 0) == 0:
        await update.message.reply_text("Kam se kam 1 sawaal toh jodiye!")
        return ADDING_QUESTIONS
        
    timers = [["10 sec", "15 sec"], ["30 sec", "1 min"], ["2 min", "5 min"]]
    await update.message.reply_text("⏰ Choose time limit per question:", reply_markup=ReplyKeyboardMarkup(timers, resize_keyboard=True, one_time_keyboard=True))
    return TIMER

async def process_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    t_text = update.message.text
    mapping = {"10 sec": 10, "15 sec": 15, "30 sec": 30, "1 min": 60, "2 min": 120, "5 min": 300}
    seconds = mapping.get(t_text, 30)
    context.user_data["c_timer"] = seconds
    
    shuffles = [["Shuffle All", "No Shuffle"], ["Only Questions", "Only Options"]]
    await update.message.reply_text("🔀 Shuffling options select karein:", reply_markup=ReplyKeyboardMarkup(shuffles, resize_keyboard=True, one_time_keyboard=True))
    return SHUFFLE

async def process_shuffle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    s_text = update.message.text
    mapping = {"Shuffle All": "all", "No Shuffle": "none", "Only Questions": "questions", "Only Options": "options"}
    shuffle_mode = mapping.get(s_text, "none")
    
    qid = context.user_data["c_qid"]
    finalize_quiz_settings(qid, context.user_data["c_timer"], shuffle_mode)
    
    share_url = f"https://t.me{BOT_USERNAME}?start=quiz_{qid}"
    await update.message.reply_text(
        f"🏁 *Quiz Ready!*\n\n"
        f"🔗 *Shareable Link:* {share_url}",
        reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
    )
    return CHOOSING

# ---- LIVE PLAYING LOGIC ----
async def start_play_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    await send_next_poll(query.from_user.id, context)

async def poll_background_timer(chat_id, poll_id, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(context.user_data["timer"])
    if "active_poll_id" in context.user_data and context.user_data["active_poll_id"] == poll_id:
        await context.bot.send_message(chat_id=chat_id, text="⏰ *Time Up!*", parse_mode="Markdown")
