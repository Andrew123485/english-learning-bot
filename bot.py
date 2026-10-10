import os
import json
import asyncio
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv
import time
import matplotlib.pyplot as plt
import io
import logging
from access_control import (
    init_access_control, check_free_limit, increment_free_usage,
    setup_access_handlers, is_premium, is_banned, get_paywall_text, get_paywall_keyboard
)
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO)

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InputFile, FSInputFile, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')

PROXY = os.getenv('PROXY', "socks4://127.0.0.1:10808")
from aiogram.client.session.aiohttp import AiohttpSession
session_http = AiohttpSession(proxy=PROXY) if PROXY else None
bot = Bot(token=BOT_TOKEN, session=session_http)
dp = Dispatcher()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
init_access_control(BASE_DIR)
# Register payment & admin handlers directly on dp FIRST
setup_access_handlers(dp, bot)

db_path = os.path.join(BASE_DIR, 'questions_db.json')

with open(db_path, 'r', encoding='utf-8') as f:
    questions = json.load(f)

blocks_order = []
for q in questions:
    if q['block_name'] not in blocks_order:
        blocks_order.append(q['block_name'])

print(f"Loaded {len(questions)} items across {len(blocks_order)} sections")

SKIP_BLOCKS = ["Texts with Gaps"]

# Load pre-generated explanations cache
explanations_cache_path = os.path.join(BASE_DIR, 'explanations_cache.json')
if os.path.exists(explanations_cache_path):
    with open(explanations_cache_path, 'r', encoding='utf-8') as f:
        explanations_cache = json.load(f)
    print(f"Loaded {len(explanations_cache)} pre-generated explanations")
else:
    explanations_cache = {}
    print("No explanations cache found (run batch_explanations.py to generate)")

# Gemini SDK removed as explanations are now fully cached and don't require the API

# SRS (Spaced Repetition System) intervals in days per level
SRS_INTERVALS = [1, 3, 7]  # Level 0→1→2 → 3 = Mastered
BREAK_EVERY = 10  # Suggest break every N questions in a session

def today_str():
    return datetime.now().strftime("%Y-%m-%d")

def srs_next_review(level, session=None):
    return ""

def srs_is_due(entry):
    return True

def migrate_errors_srs(errors: dict) -> dict:
    """Migrate old errors format to new dict format."""
    migrated = {}
    for k, v in errors.items():
        if isinstance(v, dict):
            migrated[k] = v
        else:
            migrated[k] = {"level": 0, "explanation": ""}
    return migrated

def is_placeholder(text):
    """Check if explanation is a rate-limit placeholder, not real content."""
    if not text:
        return True
    placeholders = ["_(ИИ временно", "_(Все ИИ", "_(Все доступные", "_(Ошибка ИИ", "*(Лимит запросов"]
    return any(text.strip().startswith(p) for p in placeholders)

def translate_text(text):
    """Translate English text to Russian using Google Translate (free)."""
    try:
        translator = GoogleTranslator(source='en', target='ru')
        if len(text) > 4500:
            chunks = [text[i:i+4500] for i in range(0, len(text), 4500)]
            return " ".join(translator.translate(chunk) for chunk in chunks)
        return translator.translate(text)
    except Exception as e:
        return f"(Ошибка перевода: {str(e)[:100]})"

def escape_md(text):
    """Escape Markdown special chars in user-facing text to prevent TelegramBadRequest."""
    if not text:
        return text
    # Escape characters that break Telegram Markdown parsing
    for ch in ['*', '_', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text

async def safe_send(chat_id, text, reply_markup=None):
    """Send message with Markdown, falling back to plain text on parse errors."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception:
        # Strip markdown and send as plain text
        plain = text.replace("*", "").replace("_", "").replace("`", "")
        try:
            await bot.send_message(chat_id=chat_id, text=plain, reply_markup=reply_markup)
        except Exception as e2:
            await bot.send_message(chat_id=chat_id, text=f"(Ошибка отображения: {str(e2)[:100]})", reply_markup=reply_markup)

# Global rate limit tracker — set only if ALL clients are exhausted
_global_last_exhausted_time = 0.0

def ask_gemini(question_text, correct_answer, user_answer):
    return "_(Объяснение недоступно, так как ИИ отключен для стабильности)_"

class QuizState(StatesGroup):
    answering = State()
    waiting_for_days = State()
    waiting_for_review_days = State()
    waiting_for_friend_id = State()

SESSIONS_FILE = os.path.join(BASE_DIR, 'user_sessions_english.json')

def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_sessions(sessions_dict):
    with open(SESSIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sessions_dict, f, ensure_ascii=False, indent=2)

sessions = load_sessions()

def get_session(user_id):
    str_id = str(user_id)
    if str_id not in sessions:
        sessions[str_id] = {
            "answered": [],
            "last_level": None,
            "score": 0,
            "asked": 0,
            "errors": {},
            "xp": 0,
            "streak": 0,
            "last_active_date": "",
            "answered_today": [],
            "daily_counts": {},
            "last_reset_date": ""
        }
    
    s = sessions[str_id]
    today = datetime.now().strftime("%Y-%m-%d")
    if s.get("last_reset_date") != today:
        s["answered_today"] = []
        s["daily_counts"] = {b: 0 for b in blocks_order}
        s["daily_new_done"] = 0
        s["daily_review_done"] = 0
        s["last_reset_date"] = today
        
    # Ensure daily_counts exists for old sessions
    if "daily_counts" not in s:
        s["daily_counts"] = {b: 0 for b in blocks_order}
    
    # Migrate errors to SRS format
    if s.get("errors"):
        s["errors"] = migrate_errors_srs(s["errors"])
        
    return s

def get_next_question(session, marathon_block=None, marathon_topic=None, is_first=False):
    answered = set(session.get("answered", []))
    answered_today_set = set(session.get("answered_today", []))
    # --- Marathon mode: only from chosen block ---
    if marathon_block:
        marathon_answered = set(session.get("marathon_answered", []))
        block_qs = [q for q in questions if q['block_name'] == marathon_block and q['id'] not in marathon_answered]
        if marathon_topic:
            block_qs = [q for q in block_qs if q.get('micro_topic') == marathon_topic]
        return block_qs[0] if block_qs else None
    
    # --- Calculate Targets ---
    active_questions = [q for q in questions if q.get("block_name") not in SKIP_BLOCKS]
    answered_list = session.get("answered", [])
    remaining = len(active_questions) - sum(1 for q in active_questions if q["id"] in answered_list)
    target_new = 30
    if session.get("exam_date"):
        try:
            edate = datetime.strptime(session["exam_date"], "%Y-%m-%d")
            days_left = max(1, (edate - datetime.now()).days)
            target_new = (remaining + days_left - 1) // days_left
        except:
            pass
    
    # Calculate backlog and review target
    grammar_errors = [k for k, v in session.get("errors", {}).items() if isinstance(v, dict) and v.get("level", 0) < 3]
    vocab_errors = [k for k, v in session.get("vocab_errors", {}).items() if isinstance(v, dict) and v.get("level", 0) < 3]
    
    due_grammar = [k for k in grammar_errors if srs_is_due(session["errors"][k])]
    due_vocab = [k for k in vocab_errors if srs_is_due(session["vocab_errors"][k])]
    
    target_review = len(due_grammar) + len(due_vocab)
    force_burndown = False
    
    if session.get("review_end_date"):
        try:
            rdate = datetime.strptime(session["review_end_date"], "%Y-%m-%d")
            days_left = max(1, (rdate - datetime.now()).days)
            total_errors = len(grammar_errors) + len(vocab_errors)
            target_review = (total_errors + days_left - 1) // days_left
            force_burndown = True
        except:
            pass
            
    DAILY_REVIEW = max(30, target_review) # Allow going above 30 if they need to burndown faster
    
    daily_new_done = session.get("daily_new_done", 0)
    daily_review_done = session.get("daily_review_done", 0)
    
    # --- Gather candidates from both pools ---
    new_question = None
    srs_question = None
    
    # Pool 1: New unanswered questions
    available_blocks = [b for b in blocks_order if b not in SKIP_BLOCKS]
    random.shuffle(available_blocks)
    for b in available_blocks:
        b_qs = [q for q in questions if q['block_name'] == b and q['id'] not in answered]
        if b_qs:
            new_question = b_qs[0]
            break
    
    # Pool 2: SRS review (grammar errors due) — GROUPED BY TOPIC
    errors = session.get("errors", {})
    
    # Use ALL unmastered errors for topic grouping (not just due today)
    all_grammar_ids = [q for q in grammar_errors if int(q) not in answered_today_set]
    
    # Build valid question objects with topics
    valid_srs = []
    for qid in all_grammar_ids:
        q = next((x for x in questions if x['id'] == int(qid)), None)
        if q and q['block_name'] not in SKIP_BLOCKS:
            valid_srs.append(q)
    
    if valid_srs:
        # Group by micro_topic
        current_topic = session.get("current_review_topic")
        
        if current_topic:
            # Try to continue with the same topic
            same_topic = [q for q in valid_srs if q.get("micro_topic", "Разное") == current_topic]
            if same_topic:
                srs_question = same_topic[0]
            else:
                # Topic exhausted — pick the next topic with most errors
                topic_counts = {}
                for q in valid_srs:
                    t = q.get("micro_topic", "Разное")
                    topic_counts[t] = topic_counts.get(t, 0) + 1
                best_topic = max(topic_counts, key=topic_counts.get)
                session["current_review_topic"] = best_topic
                srs_question = next(q for q in valid_srs if q.get("micro_topic", "Разное") == best_topic)
        else:
            # First time — pick the topic with most errors
            topic_counts = {}
            for q in valid_srs:
                t = q.get("micro_topic", "Разное")
                topic_counts[t] = topic_counts.get(t, 0) + 1
            best_topic = max(topic_counts, key=topic_counts.get)
            session["current_review_topic"] = best_topic
            srs_question = next(q for q in valid_srs if q.get("micro_topic", "Разное") == best_topic)
    
    # --- Pick from available candidates ---
    candidates = []
    if new_question:
        candidates.append(new_question)
    if srs_question:
        candidates.append(srs_question)
    
    if candidates:
        return random.choice(candidates)
                
    # Nothing left
    return None

def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🔥 Начать тренировку")
    builder.button(text="📚 Марафон по теме")
    builder.button(text="✍️ Работа над ошибками")
    builder.button(text="🧩 Тренировка слов")
    builder.button(text="👨‍🎓 Режим Экзамена")
    builder.button(text="📊 Моя Статистика")
    builder.button(text="📖 Моя Грамматика")
    builder.button(text="💡 Мега-Шпаргалка")
    builder.button(text="👫 Статистика друга")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)

async def send_vocab_quiz(message_obj, user_id, state, word, vocab_data):
    session = get_session(user_id)
    level = vocab_data.get("level", 0)
    
    # Get random distractors from Collocations
    collocations = [q for q in questions if q.get("block_name") == "Collocations"]
    all_answers = []
    for q in collocations:
        all_answers.extend(q["answers"])
    
    distractors = random.sample([a for a in all_answers if a["text"] != word], 3)
    options = [{
        "translation": vocab_data.get("translation", ""),
        "definition": vocab_data.get("definition", ""),
        "word": word
    }]
    for d in distractors:
        options.append({
            "translation": d.get("translation", "перевод"),
            "definition": d["match"],
            "word": d["text"]
        })
    random.shuffle(options)
    
    builder = InlineKeyboardBuilder()
    
    quiz_format = random.randint(0, 2)
    options_texts = []
    
    # Progressive difficulty by level — each level is a DIFFERENT format:
    # Level 0: format 0 (word -> pick translation) — easiest
    # Level 1: format 1 (word -> pick definition) — medium
    # Level 2: format 2 (definition -> pick word) — hardest
    if level == 0:
        quiz_format = 0
    elif level == 1:
        quiz_format = 1
    else:
        quiz_format = 2
    
    if quiz_format == 0:
        text = f"🧠 **Мини-квиз**\nКак переводится слово: **{escape_md(word)}**?"
        for i, opt in enumerate(options):
            opt_text = opt.get("translation", "перевод")
            options_texts.append(opt_text)
            cb_data = f"vcb_{1 if opt.get('word', word) == word else 0}_{i}"
            builder.button(text=opt_text, callback_data=cb_data)
    elif quiz_format == 1:
        text = f"🧠 **Мини-квиз**\nЧто означает слово **{escape_md(word)}**?"
        for i, opt in enumerate(options):
            opt_text = opt.get("definition", "def")
            options_texts.append(opt_text)
            cb_data = f"vcb_{1 if opt.get('word', word) == word else 0}_{i}"
            builder.button(text=opt_text, callback_data=cb_data)
    else:
        text = f"🧠 **Мини-квиз**\nКакое слово означает:\n*{escape_md(vocab_data.get('definition', ''))}*?"
        for i, opt in enumerate(options):
            opt_text = opt.get("word", "word")
            options_texts.append(opt_text)
            cb_data = f"vcb_{1 if opt.get('word', word) == word else 0}_{i}"
            builder.button(text=opt_text, callback_data=cb_data)
            
    builder.adjust(1)
    await state.update_data(current_vocab_word=word, current_vocab_level=level, current_vocab_format=quiz_format, current_vocab_options=options_texts)
    
    if isinstance(message_obj, Message):
        await message_obj.answer(text, parse_mode="Markdown", reply_markup=builder.as_markup())
    else:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("vcb_"))
async def handle_vocab_quiz(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = get_session(user_id)
    data = await state.get_data()
    word = data.get("current_vocab_word")
    level = data.get("current_vocab_level", 0)
    
    if not word or word not in session.get("vocab_errors", {}):
        await callback.answer("Этот вопрос уже не актуален.")
        return
        
    parts = callback.data.split("_")
    is_correct = parts[1] == "1"
    chosen_idx = int(parts[2]) if len(parts) > 2 else -1
    options_texts = data.get("current_vocab_options", [])
    chosen_text = options_texts[chosen_idx] if 0 <= chosen_idx < len(options_texts) else ""
    chosen_str = f"Ваш ответ: {escape_md(chosen_text)}\n" if chosen_text else ""
    
    quiz_format = data.get("current_vocab_format", 0)
    corr_ans = ""
    vocab_data = session.get("vocab_errors", {}).get(word, {})
    if quiz_format == 0: corr_ans = vocab_data.get("translation", "")
    elif quiz_format == 1: corr_ans = vocab_data.get("definition", "")
    else:
        tr = vocab_data.get("translation", "")
        corr_ans = f"{word} ({tr})" if tr else word
    
    if is_correct:
        new_level = level + 1
        if new_level >= 3: # 0, 1, 2 completed
            del session["vocab_errors"][word]
            await callback.message.edit_text(callback.message.text + f"\n\n{chosen_str}✅ *Верно!* Слово выучено! 🎉", parse_mode="Markdown")
        else:
            session["vocab_errors"][word]["level"] = new_level
            await callback.message.edit_text(callback.message.text + f"\n\n{chosen_str}✅ *Верно!* Уровень {new_level}/3", parse_mode="Markdown")
    else:
        new_level = max(0, level - 1)
        session["vocab_errors"][word]["level"] = new_level
        await callback.message.edit_text(callback.message.text + f"\n\n{chosen_str}❌ *Неверно!*\nПравильный ответ: {escape_md(corr_ans)}", parse_mode="Markdown")
        
    session["vocab_solved_today"] = session.get("vocab_solved_today", 0) + 1
    session["vocab_solved_total"] = session.get("vocab_solved_total", 0) + 1
    session["daily_review_done"] = session.get("daily_review_done", 0) + 1
        
    save_sessions(sessions)
    await asyncio.sleep(1.5)
    
    mode = data.get("mode", "normal")
    if mode == "vocab_only":
        await send_next_vocab_only(callback.message, user_id, state)
    else:
        await send_question(callback.message, user_id, state)

async def send_next_vocab_only(message_obj, user_id, state):
    session = get_session(user_id)
    data = await state.get_data()
    vocab = session.get("vocab_errors", {})
    
    answered = set(str(x) for x in session.get("answered", []))
    colloc_qs = [q for q in questions if q['block_name'] == 'Collocations' and str(q['id']) not in answered]
    
    # 1. Take due vocab
    due = [w for w, v in vocab.items() if srs_is_due(v)]
    unlearned = [w for w, v in vocab.items() if v.get("level", 0) < 3]
    vocab_pool = due if due else unlearned
    
    if vocab_pool:
        choice = "vocab"
    elif colloc_qs:
        choice = "colloc"
    else:
        msg = "🎉 Все слова выучены и все задания на соответствие решены!"
        if isinstance(message_obj, Message):
            await message_obj.answer(msg, reply_markup=get_main_keyboard())
        else:
            await bot.send_message(chat_id=user_id, text=msg, reply_markup=get_main_keyboard())
        await state.clear()
        return
    
    if choice == "colloc":
        q = random.choice(colloc_qs)
        # Format and send matching task
        all_match = [a['translation'] for a in q['answers']]
        if 'distractors' in q:
            all_match += q['distractors']
            
        random.shuffle(all_match)
        r_map = {str(i+1): r for i, r in enumerate(all_match)}
        
        text = f"📚 *{escape_md(q.get('topic', ''))}*\n\n" if q.get('topic') else ""
        text += "*Сопоставьте термины с определениями:*\n"
        for i, a in enumerate(q['answers']):
            text += f"*L{i+1}:* {escape_md(a['text'])}\n"
        text += "\n*Варианты:*\n"
        for k, v in r_map.items():
            text += f"*R{k}:* {escape_md(v)}\n"
            
        draft = {f"L{i+1}": None for i in range(len(q['answers']))}
        markup = build_matching_keyboard(q['id'], draft, r_map, has_hint=bool(q.get('hint')))
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, matching_draft=draft, r_map=r_map)
        increment_free_usage(user_id)
        if isinstance(message_obj, Message):
            await message_obj.answer(text, parse_mode="Markdown", reply_markup=markup)
        else:
            await safe_send(user_id, text, reply_markup=markup)
    else:
        word = random.choice(vocab_pool)
        vocab_data = vocab[word]
        await send_vocab_quiz(message_obj, user_id, state, word, vocab_data)

async def send_question(message_obj, user_id, state):
    session = get_session(user_id)
    data = await state.get_data()
    mode = data.get("mode", "normal")
    
    # === ACCESS CONTROL: Ban check ===
    if is_banned(user_id):
        await bot.send_message(chat_id=user_id, text="⛔ Ваш аккаунт заблокирован.", reply_markup=get_main_keyboard())
        await state.clear()
        return
    
    # === ACCESS CONTROL: Free limit check ===
    can_continue, remaining = check_free_limit(user_id)
    if not can_continue:
        await bot.send_message(
            chat_id=user_id,
            text=get_paywall_text(remaining),
            parse_mode="Markdown",
            reply_markup=get_paywall_keyboard()
        )
        await state.clear()
        return
    
    # --- Break reminder every BREAK_EVERY questions ---
    session_q_count = data.get("session_q_count", 0)
    if session_q_count > 0 and session_q_count % BREAK_EVERY == 0:
        await bot.send_message(
            chat_id=user_id,
            text="🧘 *Ты ответил уже на 10 вопросов подряд!*\n"
                 "Короткий перерыв поможет запомнить лучше. Выпей воды! 💧\n\n"
                 "_Следующий вопрос через 5 секунд..._",
            parse_mode="Markdown"
        )
        await asyncio.sleep(5)
    
    # Vocab errors are now handled in get_next_question (Phase 2)
    if mode == "normal":
        # Calculate dynamic targets
        active_questions = [q for q in questions if q.get("block_name") not in SKIP_BLOCKS]
        answered_list = session.get("answered", [])
        remaining = len(active_questions) - sum(1 for q in active_questions if q["id"] in answered_list)
        target_new = 30
        if session.get("exam_date"):
            try:
                edate = datetime.strptime(session["exam_date"], "%Y-%m-%d")
                days_left = (edate - datetime.now()).days
                if days_left > 0:
                    target_new = (remaining + days_left - 1) // days_left
            except:
                pass
                
        due_count = sum(1 for v in session.get("errors", {}).values() if isinstance(v, dict) and srs_is_due(v))
        daily_new = session.get("daily_new_done", 0)
        daily_rev = session.get("daily_review_done", 0)
        
        # Check if daily plan is completed (new target reached AND no pending reviews)
        if daily_new >= target_new and due_count == 0:
            await bot.send_message(chat_id=user_id, text=f"🔥 *На сегодня план выполнен!*\n\n📝 Новых: *{daily_new} / {target_new}*\n🔄 Повторений: *{daily_rev}*\n\nМолодчина! Приходи завтра за новой порцией знаний. 🇬🇧", parse_mode="Markdown", reply_markup=get_main_keyboard())
            await state.clear()
            return

        session_q_count = data.get("session_q_count", 0)
        q = get_next_question(session, is_first=(session_q_count == 0))
        if not q:
            if daily_new + daily_rev > 0:
                await bot.send_message(chat_id=user_id, text=f"🎉 *На сегодня всё!*\n📝 Новых: *{daily_new}*, 🔄 Повторений: *{daily_rev}*\nМолодец!", parse_mode="Markdown", reply_markup=get_main_keyboard())
            else:
                await bot.send_message(chat_id=user_id, text=f"Поздравляем! Вы прошли всю базу вопросов!", reply_markup=get_main_keyboard())
            await state.clear()
            return

    elif mode == "marathon":
        marathon_block = data.get("marathon_block")
        marathon_topic = data.get("marathon_topic")
        q = get_next_question(session, marathon_block=marathon_block, marathon_topic=marathon_topic)
        if not q:
            await bot.send_message(chat_id=user_id, text=f"🎉 Все вопросы в разделе *{marathon_block}* пройдены!", parse_mode="Markdown", reply_markup=get_main_keyboard())
            await state.clear()
            return
            
    elif mode == "exam":
        exam_queue = data.get("exam_queue", [])
        if not exam_queue:
            exam_score = data.get("exam_score", 0)
            total = data.get("session_q_count", 0) + 1  # +1 for last question
            pct = round(exam_score / max(total, 1) * 100)
            grade = "🏆 Отлично!" if pct >= 90 else "👍 Хорошо!" if pct >= 70 else "📖 Нужно подтянуть!"
            await bot.send_message(chat_id=user_id, text=f"🎓 *Экзамен завершен!*\n\nВаш результат: *{exam_score}/{total}* ({pct}%)\n{grade}\n\nОшибки сохранены в «Работу над ошибками».", parse_mode="Markdown", reply_markup=get_main_keyboard())
            await state.clear()
            return
        q_id = exam_queue[0]
        q = next((x for x in questions if x['id'] == q_id), None)
    else: # errors mode
        error_queue = data.get("error_queue", [])
        if not error_queue:
            await bot.send_message(chat_id=user_id, text=f"Ошибки закончились! Отличная работа!", reply_markup=get_main_keyboard())
            await state.clear()
            return
        q_id = int(error_queue[0])
        q = next((x for x in questions if x['id'] == q_id), None)

    q_id = q['id']
    topic_label = f"📚 _Тема: {escape_md(q.get('micro_topic', ''))}_\n" if q.get('micro_topic') else ""
    text = f"📍 *{q['block_name']}*\n{topic_label}\n{escape_md(q['question'])}\n\n"
    
    if q['type'] == 'single':
        answers_text = ""
        builder = InlineKeyboardBuilder()
        for i, ans in enumerate(q['answers']):
            answers_text += f"{i+1}️) {ans['text']}\n"
            builder.button(text=str(i+1), callback_data=f"ans_{q_id}_{i}")
        builder.adjust(2)
        if q.get('hint'):
            builder.row(InlineKeyboardButton(text="💡 Подсказка (AI)", callback_data=f"hint_{q_id}"))
        builder.row(InlineKeyboardButton(text="🔤 Перевод", callback_data=f"translate_{q_id}"))
        markup = builder.as_markup()
        
        # Think First: show question without options, wait 5s, then reveal
        try:
            think_msg = await bot.send_message(
                chat_id=user_id,
                text=text + "🤔 *Подумай 5 секунд...*",
                parse_mode="Markdown"
            )
        except Exception:
            think_msg = await bot.send_message(
                chat_id=user_id,
                text=text.replace("*", "") + "🤔 Подумай 5 секунд..."
            )
        await asyncio.sleep(5)
        try:
            await think_msg.edit_text(
                text=text + answers_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
        except Exception:
            # Fallback: send as new message without Markdown
            try:
                await think_msg.edit_text(
                    text=(text + answers_text).replace("*", ""),
                    reply_markup=markup
                )
            except Exception:
                await bot.send_message(chat_id=user_id, text=(text + answers_text).replace("*", ""), reply_markup=markup)
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q)
        increment_free_usage(user_id)
        
    elif q['type'] == 'matching':
        # Matching with distractors
        all_match = [a['match'] for a in q['answers']]
        if q.get('distractors'):
            all_match += q['distractors']
            
        random.shuffle(all_match)
        r_map = {str(i+1): r for i, r in enumerate(all_match)}
        
        text += "*Сопоставьте термины с определениями:*\n"
        for i, a in enumerate(q['answers']):
            text += f"*L{i+1}:* {escape_md(a['text'])}\n"
        text += "\n*Варианты:*\n"
        for k, v in r_map.items():
            text += f"*R{k}:* {escape_md(v)}\n"
            
        draft = {f"L{i+1}": None for i in range(len(q['answers']))}
        markup = build_matching_keyboard(q_id, draft, r_map, has_hint=bool(q.get('hint')))
        await state.set_state(QuizState.answering)
        await state.update_data(current_q=q, matching_draft=draft, r_map=r_map)
        increment_free_usage(user_id)
        await safe_send(user_id, text, reply_markup=markup)

def build_matching_keyboard(q_id, draft, r_map, has_hint=False):
    builder = InlineKeyboardBuilder()
    for L_key in draft.keys():
        current_sel = draft[L_key]
        for ri in r_map.keys():
            label = f"{L_key}-R{ri}"
            if current_sel == ri: label = f"✅ {L_key}-R{ri}"
            builder.button(text=label, callback_data=f"match_{q_id}_{L_key}_{ri}")
    builder.adjust(len(r_map))
    builder.row(InlineKeyboardButton(text="Ответить ➡️", callback_data=f"matchsub_{q_id}"))
    builder.row(InlineKeyboardButton(text="⏭ Пропустить (Не знаю)", callback_data=f"matchskip_{q_id}"))
    if has_hint:
        builder.row(InlineKeyboardButton(text="💡 Подсказка (AI)", callback_data=f"hint_{q_id}"))
    builder.row(InlineKeyboardButton(text="🔤 Перевод", callback_data=f"translate_{q_id}"))
    return builder.as_markup()

@dp.callback_query(F.data.startswith("ans_"))
async def handle_ans(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data.get("current_q")
    if not q:
        await callback.answer("Текущий вопрос не найден.")
        return
    
    parts = callback.data.split('_')
    cb_q_id = int(parts[1])
    
    # Anti-collision: check if this is the actual current question
    if cb_q_id != q['id']:
        await callback.answer("Этот вопрос уже неактуален.", show_alert=True)
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except: pass
        return

    ans_idx = int(parts[2])
    selected = q['answers'][ans_idx]
    
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await process_answer(callback.message, callback.from_user.id, state, q, selected['text'], selected['is_correct'])

@dp.callback_query(F.data.startswith("match_"))
async def handle_match(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q, draft, r_map = data.get("current_q"), data.get("matching_draft"), data.get("r_map")
    if not q: 
        await callback.answer("Вопрос не найден.")
        return
        
    parts = callback.data.split('_')
    cb_q_id = int(parts[1])
    if cb_q_id != q['id']:
        await callback.answer("Это кнопки от старого вопроса.")
        try: await callback.message.edit_reply_markup(reply_markup=None)
        except: pass
        return

    L, R = parts[2], parts[3]
    draft[L] = R if draft.get(L) != R else None
    await state.update_data(matching_draft=draft)
    try:
        await callback.message.edit_reply_markup(reply_markup=build_matching_keyboard(q['id'], draft, r_map))
    except Exception as e:
        print(f"Edit markup error: {e}")
    await callback.answer()

@dp.callback_query(F.data.startswith("matchskip_"))
async def handle_match_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q, draft, r_map = data.get("current_q"), data.get("matching_draft"), data.get("r_map")
    
    if not q: return
    cb_q_id = int(callback.data.split('_')[1])
    if cb_q_id != q['id']:
        await callback.answer("Этот вопрос уже не актуален.")
        return

    is_correct = False
    user_ans_lines = ["Пропущено"]
    per_answer_feedback = []
    failed_sub_items = q['answers']
    
    for i, a in enumerate(q['answers']):
        per_answer_feedback.append(f"❌ *{a['text']}* → Не выбрано")
        
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await process_answer(callback.message, callback.from_user.id, state, q, "\n".join(user_ans_lines), is_correct, per_answer_feedback=per_answer_feedback, failed_sub_items=failed_sub_items)

@dp.callback_query(F.data.startswith("matchsub_"))
async def handle_match_sub(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q, draft, r_map = data.get("current_q"), data.get("matching_draft"), data.get("r_map")
    
    if not q: return
    cb_q_id = int(callback.data.split('_')[1])
    if cb_q_id != q['id']:
        await callback.answer("Этот вопрос уже не актуален.")
        return

    if None in draft.values():
        await callback.answer("Соедините все пары!", show_alert=True)
        return
    
    is_correct = True
    user_ans_lines = []
    per_answer_feedback = []
    failed_sub_items = []
    for i, a in enumerate(q['answers']):
        selected_R = r_map[draft[f"L{i+1}"]]
        pair_correct = (selected_R == a['match'])
        if not pair_correct:
            is_correct = False
            failed_sub_items.append(a)
        icon = "✅" if pair_correct else "❌"
        user_ans_lines.append(f"{a['text']} -> {selected_R}")
        per_answer_feedback.append(f"{icon} *{a['text']}* → {selected_R}")
        
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await process_answer(callback.message, callback.from_user.id, state, q, "\n".join(user_ans_lines), is_correct, per_answer_feedback=per_answer_feedback, failed_sub_items=failed_sub_items)

async def process_answer(message_obj, user_id, state, q_obj, user_ans, is_correct, per_answer_feedback=None, failed_sub_items=None):
    session = get_session(user_id)
    data = await state.get_data()
    mode = data.get("mode", "normal")
    q_id_str = str(q_obj['id'])
    if mode in ("normal", "marathon"):
        if q_obj['id'] not in session["answered"]:
            session["answered"].append(q_obj['id'])
        if mode == "marathon":
            if "marathon_answered" not in session:
                session["marathon_answered"] = []
            session["marathon_answered"].append(q_obj['id'])
        if q_obj['id'] not in session.get("answered_today", []):
            session["answered_today"].append(q_obj['id'])
            b = q_obj['block_name']
            session["daily_counts"][b] = session["daily_counts"].get(b, 0) + 1
            # Track new vs review for daily limits
            q_id_str_track = str(q_obj['id'])
            if q_id_str_track in session.get("errors", {}):
                session["daily_review_done"] = session.get("daily_review_done", 0) + 1
            else:
                session["daily_new_done"] = session.get("daily_new_done", 0) + 1
            
    translate_kb = InlineKeyboardBuilder()
    translate_kb.button(text="🔤 Перевод", callback_data=f"translate_{q_id_str}")
    translate_markup = translate_kb.as_markup()
    
    if is_correct:
        session["xp"] += 10
        if q_id_str in session["errors"]:
            del session["errors"][q_id_str]
            await bot.send_message(chat_id=user_id, text=
                f"✅ *Верно! Отлично!*\n\n*Твой выбор:* ✅ {escape_md(user_ans)}\n\n🌟 *Вы исправили ошибку, вопрос выучен!*",
                parse_mode="Markdown", reply_markup=translate_markup)
            session["daily_srs_done"] = session.get("daily_srs_done", 0) + 1
        else:
            await bot.send_message(chat_id=user_id, text=f"✅ *Верно! Отлично!*\n\n*Твой выбор:* ✅ {escape_md(user_ans)}", parse_mode="Markdown", reply_markup=translate_markup)
    else:
        if q_obj.get('block_name') == 'Collocations' and failed_sub_items:
            if "vocab_errors" not in session:
                session["vocab_errors"] = {}
            for item in failed_sub_items:
                word = item['text']
                if word not in session["vocab_errors"]:
                    session["vocab_errors"][word] = {
                        "level": 0,
                        "translation": item.get("translation", ""),
                        "definition": item["match"]
                    }
                else:
                    cur = session["vocab_errors"][word].get("level", 0)
                    session["vocab_errors"][word]["level"] = max(0, cur - 1)
        else:
            # Get existing SRS entry or create new one
            existing = session["errors"].get(q_id_str, {})
            if not isinstance(existing, dict):
                existing = {"level": 0, "explanation": ""}
            
            explanation_cached = existing.get("explanation", "")
            
            # Wrong answer → put back into errors
            session["errors"][q_id_str] = {
                "level": 0,
                "explanation": explanation_cached
            }
        
        # Build correct answer display
        if q_obj['type'] == 'single':
            correct_text = "\n".join([a['text'] for a in q_obj['answers'] if a.get('is_correct')])
        else:
            correct_text = "\n".join([f"{a['text']} ({a.get('translation')}) → {a['match']}" if a.get('translation') else f"{a['text']} → {a['match']}" for a in q_obj['answers']])
        
        if per_answer_feedback:
            feedback_block = "\n".join(per_answer_feedback)
            result_msg = f"❌ *Не всё верно!*\n\n*Твой выбор:*\n{escape_md(feedback_block)}\n\n*Правильные ответы:*\n{escape_md(correct_text)}"
        else:
            result_msg = f"❌ *Неверно!*\n\n*Твой выбор:* ❌ {escape_md(user_ans)}\n\n*Правильный ответ:* ✅ {escape_md(correct_text)}"
        
        # Explanation priority: 1) pre-generated cache  2) session cache  3) button
        cached_explanation = explanations_cache.get(q_id_str, "")
        session_explanation = (session.get("errors", {}).get(q_id_str, {}) or {}).get("explanation", "")
        
        if cached_explanation and not is_placeholder(cached_explanation):
            await safe_send(user_id, result_msg + f"\n\n*💡 Разбор:*\n{escape_md(cached_explanation)}", reply_markup=translate_markup)
        elif session_explanation and not is_placeholder(session_explanation):
            await safe_send(user_id, result_msg + f"\n\n*💡 Объяснение:*\n{escape_md(session_explanation)}", reply_markup=translate_markup)
        elif q_obj['block_name'] == 'Texts with Gaps':
            await safe_send(user_id, result_msg, reply_markup=translate_markup)
        else:
            # No cache — show explain + translate buttons
            explain_builder = InlineKeyboardBuilder()
            explain_builder.button(text="💡 Объяснить ошибку", callback_data=f"explain_{q_id_str}")
            explain_builder.button(text="🔤 Перевод", callback_data=f"translate_{q_id_str}")
            explain_builder.adjust(1)
            await safe_send(user_id, result_msg, reply_markup=explain_builder.as_markup())

    if mode == "exam":
        exam_q = data.get("exam_queue", [])
        if exam_q: exam_q.pop(0)
        await state.update_data(exam_queue=exam_q, exam_score=data.get("exam_score", 0) + (1 if is_correct else 0))
    elif mode == "errors":
        err_q = data.get("error_queue", [])
        if err_q: err_q.pop(0)
        await state.update_data(error_queue=err_q)

    # Track session question count for break reminders
    await state.update_data(session_q_count=data.get("session_q_count", 0) + 1)
    save_sessions(sessions)
    await asyncio.sleep(1)
    
    if mode == "vocab_only":
        await send_next_vocab_only(message_obj, user_id, state)
    else:
        await send_question(message_obj, user_id, state)

async def process_days_input(message: Message, state: FSMContext, days_str: str):
    if not days_str.isdigit():
        await message.answer("Пожалуйста, введи только число (например, 17):")
        return
    
    days = int(days_str)
    if days <= 0 or days > 365:
        await message.answer("Пожалуйста, введи реальное количество дней (от 1 до 365):")
        return
        
    exam_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    session = get_session(message.from_user.id)
    session["exam_date"] = exam_date
    save_sessions(sessions)
    
    await message.answer(f"✅ Отлично! Твой экзамен примерно {exam_date}.\nВ разделе «📊 Моя Статистика» теперь отображается твоя дневная норма вопросов.", reply_markup=get_main_keyboard())
    await state.clear()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        await process_days_input(message, state, args[1])
        return

    if not session.get("exam_date"):
        await message.answer("🇬🇧 Добро пожаловать! Я помогу тебе подготовиться к экзамену.\n\nЧерез сколько дней у тебя экзамен? (Напиши просто число, например: 17)")
        await state.set_state(QuizState.waiting_for_days)
    else:
        await message.answer("🇬🇧 Добро пожаловать! Я помогу тебе подготовиться к экзамену по английскому.", reply_markup=get_main_keyboard())
        await state.clear()

@dp.message(Command("set_date"))
async def cmd_set_date(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        await process_days_input(message, state, args[1])
        return

    await message.answer("Через сколько дней у тебя экзамен? (Напиши просто число, например: 17)")
    await state.set_state(QuizState.waiting_for_days)

@dp.message(QuizState.waiting_for_days)
async def handle_setup_days(message: Message, state: FSMContext):
    await process_days_input(message, state, message.text.strip())

async def process_review_days_input(message: Message, state: FSMContext, days_str: str):
    if not days_str.isdigit():
        await message.answer("Пожалуйста, введи только число (например, 7):")
        return
    
    days = int(days_str)
    if days <= 0 or days > 365:
        await message.answer("Пожалуйста, введи реальное количество дней (от 1 до 365):")
        return
        
    review_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    session = get_session(message.from_user.id)
    session["review_end_date"] = review_date
    save_sessions(sessions)
    
    await message.answer(f"✅ Готово! Твоя цель закрыть все ошибки до {review_date}.\nЕжедневная норма обновлена в статистике.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(Command("set_review_date"))
async def cmd_set_review_date(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        await process_review_days_input(message, state, args[1])
        return

    await message.answer("Через сколько дней ты хочешь полностью закончить со всеми ошибками? (Напиши просто число, например: 7)")
    await state.set_state(QuizState.waiting_for_review_days)

@dp.message(QuizState.waiting_for_review_days)
async def handle_setup_review_days(message: Message, state: FSMContext):
    await process_review_days_input(message, state, message.text.strip())

@dp.message(F.text == "🔥 Начать тренировку")
async def cmd_train(message: Message, state: FSMContext):
    await state.update_data(mode="normal", session_q_count=0)
    await send_question(message, message.from_user.id, state)

@dp.message(F.text == "🧩 Тренировка слов")
async def cmd_vocab_only(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    vocab = session.get("vocab_errors", {})
    if not vocab:
        await message.answer("📖 Твой словарь пока пуст! Решай задания раздела Collocations, чтобы пополнять его.", reply_markup=get_main_keyboard())
        return
    await state.update_data(mode="vocab_only")
    await send_next_vocab_only(message, message.from_user.id, state)

@dp.message(F.text == "\u270d\ufe0f \u0420\u0430\u0431\u043e\u0442\u0430 \u043d\u0430\u0434 \u043e\u0448\u0438\u0431\u043a\u0430\u043c\u0438")
async def cmd_errors(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    errors = session.get("errors", {})
    if not errors:
        await message.answer("\u041e\u0448\u0438\u0431\u043e\u043a \u043f\u043e\u043a\u0430 \u043d\u0435\u0442!")
        return
    
    # Filter out SKIP_BLOCKS (like Texts with Gaps) from error review
    valid_error_ids = []
    for k in errors:
        q = next((x for x in questions if x['id'] == int(k)), None)
        if q and q['block_name'] not in SKIP_BLOCKS:
            valid_error_ids.append(k)

    due_ids = [k for k in valid_error_ids if srs_is_due(errors[k])]
    future_ids = [k for k in valid_error_ids if k not in due_ids]
    
    if not due_ids:
        # Tell user when next review is
        next_dates = sorted(set(v.get("next_review", "?") for v in errors.values() if isinstance(v, dict)))
        await message.answer(
            f"\U0001f7e2 *\u041d\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u043d\u0435\u0442 \u043a\u0430\u0440\u0442\u043e\u0447\u0435\u043a \u0434\u043b\u044f \u043f\u043e\u0432\u0442\u043e\u0440\u0435\u043d\u0438\u044f!*\n"
            f"\U0001f4c5 \u0411\u043b\u0438\u0436\u0430\u0439\u0448\u0438\u0439 \u043f\u043e\u0432\u0442\u043e\u0440: *{next_dates[0]}*\n"
            f"\u0412\u0441\u0435\u0433\u043e \u0432 \u0440\u0430\u0431\u043e\u0442\u0435: *{len(errors)}* \u043a\u0430\u0440\u0442\u043e\u0447\u0435\u043a",
            parse_mode="Markdown"
        )
        return
    
    await message.answer(
        f"\u270d\ufe0f *\u0420\u0430\u0431\u043e\u0442\u0430 \u043d\u0430\u0434 \u043e\u0448\u0438\u0431\u043a\u0430\u043c\u0438*\n"
        f"\U0001f534 \u0421\u0435\u0433\u043e\u0434\u043d\u044f \u043a \u043f\u043e\u0432\u0442\u043e\u0440\u0435\u043d\u0438\u044e: *{len(due_ids)}* \u043a\u0430\u0440\u0442\u043e\u0447\u0435\u043a\n"
        f"\U0001f4c5 \u0416\u0434\u0443\u0442 \u0432 \u0431\u0443\u0434\u0443\u0449\u0435\u043c: *{len(future_ids)}*",
        parse_mode="Markdown"
    )
    await state.update_data(mode="errors", error_queue=due_ids, session_q_count=0)
    await send_question(message, message.from_user.id, state)

@dp.message(F.text == "👨‍🎓 Режим Экзамена")
async def cmd_exam(message: Message, state: FSMContext):
    # Filter questions by type
    colloc_qs = [q for q in questions if q.get("block_name") == "Collocations"]
    texts_qs = [q for q in questions if q.get("block_name") == "Texts with Gaps"]
    
    gv_qs = [q for q in questions if q.get("block_name") == "Grammar & Vocabulary"]
    lexis_qs = [q for q in gv_qs if q.get("micro_topic") and "Лексика" in q.get("micro_topic", "")]
    grammar_qs = [q for q in gv_qs if not (q.get("micro_topic") and "Лексика" in q.get("micro_topic", ""))]
    
    exam_qs = []
    exam_qs.extend(random.sample(colloc_qs, min(4, len(colloc_qs))))
    exam_qs.extend(random.sample(texts_qs, min(1, len(texts_qs))))
    exam_qs.extend(random.sample(grammar_qs, min(10, len(grammar_qs))))
    exam_qs.extend(random.sample(lexis_qs, min(10, len(lexis_qs))))
    
    # Shuffle the exam queue so questions appear in random order
    random.shuffle(exam_qs)
    
    exam_queue = [q['id'] for q in exam_qs]
    
    await message.answer(f"🎓 *Режим Экзамена!*\nЭкзамен состоит из {len(exam_queue)} заданий. Если ответишь неверно — задание отправится в «Работу над ошибками». Удачи!", parse_mode="Markdown")
    await state.update_data(mode="exam", exam_queue=exam_queue, exam_score=0, session_q_count=0)
    await send_question(message, message.from_user.id, state)

@dp.message(F.text == "\U0001f4ca Моя Статистика")
async def cmd_stats(message: Message):
    session = get_session(message.from_user.id)
    answered = set(session.get("answered", []))
    active_questions = [q for q in questions if q.get("block_name") not in SKIP_BLOCKS]
    total = len(active_questions)
    done = sum(1 for q in active_questions if q["id"] in answered)
    remaining = total - done
    errors = len(session.get("errors", {}))
    xp = session.get("xp", 0)
    today_done = len(session.get("answered_today", []))

    # Per-block counts
    block_stats = {}
    for b in blocks_order:
        b_total = sum(1 for q in questions if q['block_name'] == b)
        b_done = sum(1 for q in questions if q['block_name'] == b and q['id'] in answered)
        block_stats[b] = (b_done, b_total)

    # Build the chart
    fig, axes = plt.subplots(1, 2, figsize=(10, 5), facecolor='#1a1a2e')

    # Left: overall pie
    ax1 = axes[0]
    ax1.set_facecolor('#1a1a2e')
    wedges, texts, autotexts = ax1.pie(
        [done, remaining],
        labels=['Пройдено', 'Осталось'],
        colors=['#4ade80', '#334155'],
        explode=(0.05, 0),
        autopct='%1.1f%%',
        startangle=90,
        textprops={'color': 'white', 'fontsize': 12},
        wedgeprops={'linewidth': 2, 'edgecolor': '#1a1a2e'}
    )
    for at in autotexts:
        at.set_color('white')
        at.set_fontweight('bold')
    ax1.set_title(f'Общий прогресс\n{done} / {total}', color='white', fontsize=13, fontweight='bold', pad=15)

    # Right: per-block stacked bar
    ax2 = axes[1]
    ax2.set_facecolor('#1a1a2e')
    short_names = {'Grammar & Vocabulary': 'Грамматика', 'Collocations': 'Соотношение', 'Texts with Gaps': 'Тексты'}
    labels = [short_names.get(b, b) for b in blocks_order]
    done_vals = [block_stats[b][0] for b in blocks_order]
    total_vals = [block_stats[b][1] for b in blocks_order]
    rem_vals = [t - d for d, t in zip(done_vals, total_vals)]
    bar_colors = ['#4ade80', '#60a5fa', '#f472b6']
    x = list(range(len(labels)))

    bars1 = ax2.bar(x, done_vals, color=bar_colors, alpha=0.9, label='Пройдено', edgecolor='#1a1a2e', linewidth=1.5)
    ax2.bar(x, rem_vals, bottom=done_vals, color='#334155', alpha=0.7, label='Осталось', edgecolor='#1a1a2e', linewidth=1.5)

    ax2.set_xticks(x)
    ax2.set_xticklabels(labels, color='white', fontsize=10)
    ax2.tick_params(axis='y', colors='white')
    for spine in ['bottom', 'left']:
        ax2.spines[spine].set_color('#475569')
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.set_title('По разделам', color='white', fontsize=13, fontweight='bold', pad=15)
    ax2.legend(facecolor='#334155', edgecolor='none', labelcolor='white', fontsize=9)

    for bar, d, t in zip(bars1, done_vals, total_vals):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() / 2,
                 f'{d}/{t}', ha='center', va='center', color='white', fontsize=9, fontweight='bold')

    plt.tight_layout(pad=2)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='#1a1a2e')
    buf.seek(0)
    plt.close(fig)

    errors = session.get("errors", {})
    due_count = sum(1 for v in errors.values() if isinstance(v, dict) and srs_is_due(v))
    total_srs = len(errors)
    
    vocab_errors = session.get("vocab_errors", {})
    vocab_due = sum(1 for v in vocab_errors.values() if isinstance(v, dict) and srs_is_due(v))
    vocab_total = len(vocab_errors)
    
    exam_info = ""
    target_new = 30
    if session.get("exam_date"):
        try:
            edate = datetime.strptime(session["exam_date"], "%Y-%m-%d")
            days_left = (edate - datetime.now()).days
            if days_left > 0:
                target_new = (remaining + days_left - 1) // days_left
                exam_info = f"📅 До экзамена: *{days_left} дней*\n"
            else:
                exam_info = "📅 Экзамен уже сегодня или прошел!\n"
        except:
            pass
            
    target_review = due_count + vocab_due
    review_info = ""
    if session.get("review_end_date"):
        try:
            rdate = datetime.strptime(session["review_end_date"], "%Y-%m-%d")
            rdays_left = (rdate - datetime.now()).days
            if rdays_left > 0:
                grammar_unlearned = sum(1 for v in errors.values() if isinstance(v, dict) and v.get("level", 0) < 3)
                vocab_unlearned = sum(1 for v in vocab_errors.values() if isinstance(v, dict) and v.get("level", 0) < 3)
                target_review = (grammar_unlearned + vocab_unlearned + rdays_left - 1) // rdays_left
                review_info = f"🎯 До завершения работы над ошибками: *{rdays_left} дней*\n"
            else:
                review_info = "🎯 Работа над ошибками должна быть завершена!\n"
        except:
            pass

    caption = (
        f"📊 *Статистика подготовки*\n\n"
        f"{exam_info}"
        f"{review_info}"
        f"✅ Пройдено: *{done}* / {total} (Осталось: {remaining})\n"
        f"🧠 SRS грамматика: *{total_srs}* (на сегодня: *{due_count}*)\n"
        f"📖 SRS слова: *{vocab_total}* (на сегодня: *{vocab_due}*)\n"
        f"📝 Решено квизов по словам: *{session.get('vocab_solved_total', 0)}*\n"
        f"🏆 Опыт: *{xp} XP*\n\n"
        f"🎯 *Цель на сегодня:*\n"
        f"• Изучить новых: *{session.get('daily_new_done', 0)} / {target_new}*\n"
        f"• Повторить старых: *{session.get('daily_review_done', 0)} / {target_review}*"
    )
    await message.answer_photo(
        photo=BufferedInputFile(buf.getvalue(), filename="stats.png"), 
        caption=caption, 
        parse_mode="Markdown"
    )


@dp.message(F.text == "📖 Моя Грамматика")
async def cmd_theory(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("⭐️ Моя Грамматика доступна только по Premium подписке!", reply_markup=get_paywall_keyboard())
        return
        
    session = get_session(message.from_user.id)
    errors = session.get("errors", {})
    
    import theory_generator
    import json
    
    try:
        with open('explanations_cache.json', 'r', encoding='utf-8') as f:
            cache = json.load(f)
            
        filename = theory_generator.generate_user_theory(message.from_user.id, errors, questions, cache)
        
        doc = FSInputFile(filename)
        await message.answer_document(doc, caption="📖 Твой персональный справочник по грамматике и лексике, собранный по твоим текущим ошибкам!")
        
        # Clean up the temp docx
        import os
        if os.path.exists(filename):
            os.remove(filename)
            
    except Exception as e:
        await message.answer(f"Не удалось создать справочник: {str(e)[:100]}")


@dp.message(F.text == "💡 Мега-Шпаргалка")
async def cmd_cheat_sheet(message: Message):
    if not is_premium(message.from_user.id):
        await message.answer("⭐️ Мега-Шпаргалка доступна только по Premium подписке!", reply_markup=get_paywall_keyboard())
        return
        
    sheet_path = os.path.join(BASE_DIR, 'cheat_sheet.docx')
    if not os.path.exists(sheet_path):
        await message.answer("Шпаргалка пока недоступна.")
        return
    
    doc = FSInputFile(sheet_path)
    await message.answer_document(doc, caption="💡 Вот твоя Мега-Шпаргалка со всеми секретами и лайфхаками для тестов!")

@dp.message(F.text == "📚 Марафон по теме")
async def cmd_marathon(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    answered = set(session.get("answered", []))
    
    builder = InlineKeyboardBuilder()
    for i, b in enumerate(blocks_order):
        b_total = sum(1 for q in questions if q['block_name'] == b)
        b_done = sum(1 for q in questions if q['block_name'] == b and q['id'] in answered)
        short = {'Grammar & Vocabulary': '📖 Грамматика', 'Collocations': '🔗 Соотношения', 'Texts with Gaps': '📝 Тексты'}
        label = f"{short.get(b, b)} ({b_done}/{b_total})"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"marathon_{i}"))
        
        if b == 'Grammar & Vocabulary':
            topics = set(q.get('micro_topic') for q in questions if q['block_name'] == b and q.get('micro_topic'))
            for t in sorted(list(topics)):
                t_total = sum(1 for q in questions if q['block_name'] == b and q.get('micro_topic') == t)
                t_done = sum(1 for q in questions if q['block_name'] == b and q.get('micro_topic') == t and q['id'] in answered)
                if t_total > 0:
                    builder.row(InlineKeyboardButton(text=f"▫️ {t} ({t_done}/{t_total})", callback_data=f"martop_{t}"))
    
    await message.answer(
        "📚 *Марафон по теме*\n\n"
        "Выберите раздел — вопросы пойдут последовательно из одного блока.\n"
        "Так проще сфокусироваться и запомнить!",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("marathon_"))
async def handle_marathon_select(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[1])
    block = blocks_order[idx]
    await state.update_data(mode="marathon", marathon_block=block, marathon_topic=None, session_q_count=0)
    session = get_session(callback.from_user.id)
    session["marathon_answered"] = []
    await callback.answer(f"Марафон: {block}")
    await callback.message.edit_reply_markup(reply_markup=None)
    await send_question(callback.message, callback.from_user.id, state)

@dp.callback_query(F.data.startswith("martop_"))
async def handle_marathon_topic_select(callback: CallbackQuery, state: FSMContext):
    topic = callback.data.split("_", 1)[1]
    block = "Grammar & Vocabulary"
    await state.update_data(mode="marathon", marathon_block=block, marathon_topic=topic, session_q_count=0)
    session = get_session(callback.from_user.id)
    session["marathon_answered"] = []
    await callback.answer(f"Марафон: {topic}")
    await callback.message.edit_reply_markup(reply_markup=None)
    await send_question(callback.message, callback.from_user.id, state)

@dp.callback_query(F.data.startswith("hint_"))
async def handle_hint(callback: CallbackQuery, state: FSMContext):
    q_id = int(callback.data.split('_')[1])
    q = next((x for x in questions if x['id'] == q_id), None)
    if not q or 'hint' not in q:
        await callback.answer("Подсказка для этого вопроса еще не сгенерирована ИИ. Попробуй позже!", show_alert=True)
    else:
        # Show alert with the hint
        await callback.answer(q['hint'], show_alert=True)


@dp.message(F.text == "🔧 Починить объяснения")
async def cmd_fix_explanations(message: Message, state: FSMContext):
    session = get_session(message.from_user.id)
    errors = session.get("errors", {})
    
    to_fix = [(qid, entry) for qid, entry in errors.items()
              if is_placeholder(entry.get("explanation", ""))]
    
    if not to_fix:
        await message.answer("✅ Все объяснения в порядке! Сломанных нет.")
        return
    
    msg = await message.answer(f"🔄 Восстанавливаю {len(to_fix)} объяснений... Это займёт пару минут.")
    
    fixed = 0
    for i, (qid, entry) in enumerate(to_fix):
        q = next((x for x in questions if x['id'] == int(qid)), None)
        if not q:
            continue
        
        if q['type'] == 'single':
            correct_text = "\n".join([a['text'] for a in q['answers'] if a.get('is_correct')])
        else:
            correct_text = "\n".join([f"{a['text']} → {a['match']}" for a in q['answers']])
        
        explanation = await asyncio.get_event_loop().run_in_executor(
            None, ask_gemini, q['question'], correct_text, "(неизвестно)"
        )
        
        if not is_placeholder(explanation):
            session["errors"][qid]["explanation"] = explanation
            fixed += 1
        
        if (i + 1) % 5 == 0 or (i + 1) == len(to_fix):
            try:
                await msg.edit_text(f"🔄 Восстановлено {fixed}/{len(to_fix)} объяснений... ({i+1}/{len(to_fix)} обработано)")
            except:
                pass
        
        await asyncio.sleep(3)
    
    save_sessions(sessions)
    try:
        await msg.edit_text(f"✅ Готово! Восстановлено *{fixed}* из {len(to_fix)} объяснений.", parse_mode="Markdown")
    except:
        pass


@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, сбросить", callback_data="confirm_reset")
    builder.button(text="❌ Отмена", callback_data="cancel_reset")
    builder.adjust(2)
    
    session = get_session(message.from_user.id)
    answered_count = len(session.get("answered", []))
    errors_count = len(session.get("errors", {}))
    
    await message.answer(
        f"⚠️ *Сбросить прогресс?*\n\n"
        f"Пройдено вопросов: *{answered_count}*\n"
        f"Ошибок (SRS): *{errors_count}*\n\n"
        f"Список пройденных вопросов обнулится — начнёте сначала.\n"
        f"Ошибки и SRS-карточки *останутся* для повторения.",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "confirm_reset")
async def handle_confirm_reset(callback: CallbackQuery, state: FSMContext):
    session = get_session(callback.from_user.id)
    old_count = len(session.get("answered", []))
    old_errors = len(session.get("errors", {}))
    session["answered"] = []
    session["answered_today"] = []
    session["daily_counts"] = {}
    session["errors"] = {}
    session["score"] = 0
    session["asked"] = 0
    session["xp"] = 0
    session["session_q_count"] = 0
    session["daily_srs_done"] = 0
    session["daily_new_done"] = 0
    session["daily_review_done"] = 0
    save_sessions(sessions)
    
    # Clear FSM state so quiz stops and user can start fresh
    await state.clear()
    
    await callback.answer("Прогресс сброшен!")
    await callback.message.edit_text(
        f"🔄 *Прогресс полностью сброшен!*\n"
        f"Обнулено: *{old_count}* вопросов, *{old_errors}* ошибок.\n"
        f"Всё с чистого листа!\n\n"
        f"Нажми «🔥 Начать тренировку» чтобы начать с первого вопроса!",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "cancel_reset")
async def handle_cancel_reset(callback: CallbackQuery):
    await callback.answer("Отменено")
    await callback.message.edit_text("❌ Отменено. Прогресс не изменён.")


@dp.callback_query(F.data.startswith("explain_"))
async def handle_explain(callback: CallbackQuery):
    q_id_str = callback.data.split("_", 1)[1]
    session = get_session(callback.from_user.id)
    
    q = next((x for x in questions if x['id'] == int(q_id_str)), None)
    if not q:
        await callback.answer("Вопрос не найден.")
        return
    
    await callback.message.edit_reply_markup(reply_markup=None)
    
    # 1) Check pre-generated cache first (no API call!)
    if q_id_str in explanations_cache:
        explanation = explanations_cache[q_id_str]
        await callback.answer()
    # 2) Check session cache
    elif q_id_str in session.get("errors", {}) and not is_placeholder(session["errors"][q_id_str].get("explanation", "")):
        explanation = session["errors"][q_id_str]["explanation"]
        await callback.answer()
    else:
        # 3) Try Gemini, but check if keys are available first
        current_time = time.time()
        available = [i for i, cd in enumerate(_clients_cooldowns) if cd <= current_time]
        
        if available:
            await callback.answer("Генерирую объяснение...")
            if q['type'] == 'single':
                correct_text = "\n".join([a['text'] for a in q['answers'] if a.get('is_correct')])
            else:
                correct_text = "\n".join([f"{a['text']} → {a['match']}" for a in q['answers']])
            
            explanation = await asyncio.get_event_loop().run_in_executor(
                None, ask_gemini, q['question'], correct_text, "(неизвестно)"
            )
        else:
            # All keys exhausted — use Google Translate as fallback
            await callback.answer("ИИ недоступен, делаю перевод...")
            if q['type'] == 'single':
                correct_text = "\n".join([a['text'] for a in q['answers'] if a.get('is_correct')])
            else:
                correct_text = "\n".join([f"{a['text']} → {a['match']}" for a in q['answers']])
            
            to_translate = f"Question: {q['question']}\nCorrect answer: {correct_text}"
            translation = await asyncio.get_event_loop().run_in_executor(
                None, translate_text, to_translate
            )
            explanation = f"_(ИИ временно недоступен. Вот перевод:)_\n\n{translation}"
    
    # Save to session errors if available
    if not is_placeholder(explanation):
        if q_id_str in session.get("errors", {}):
            session["errors"][q_id_str]["explanation"] = explanation
            save_sessions(sessions)
    
    await safe_send(
        chat_id=callback.from_user.id,
        text=f"💡 *Объяснение:*\n{explanation}"
    )


@dp.callback_query(F.data.startswith("translate_"))
async def handle_translate(callback: CallbackQuery):
    q_id = int(callback.data.split("_")[1])
    q = next((x for x in questions if x['id'] == q_id), None)
    if not q:
        await callback.answer("Вопрос не найден.")
        return
    
    await callback.answer("Перевожу...")
    
    # Build text to translate
    to_translate = q['question']
    if q['type'] == 'single':
        options = "\n".join([f"{i+1}) {a['text']}" for i, a in enumerate(q['answers'])])
        to_translate += "\n\nAnswer options:\n" + options
    elif q['type'] == 'matching':
        left = "\n".join([f"- {a['text']}" for a in q['answers']])
        right = "\n".join([f"- {a['match']}" for a in q['answers']])
        to_translate += "\n\nTerms:\n" + left + "\n\nDefinitions:\n" + right
    
    # Translate using Google Translate (free, no API key, no rate limits!)
    translation = await asyncio.get_event_loop().run_in_executor(
        None, translate_text, to_translate
    )
    
    await safe_send(
        callback.from_user.id,
        f"🔤 *Перевод:*\n\n{escape_md(translation)}"
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твой Telegram ID: `{message.from_user.id}`\n\nОтправь его другу, чтобы он мог посмотреть твою статистику!", parse_mode="Markdown")

@dp.message(F.text == "👫 Статистика друга")
async def cmd_friend_stats(message: Message, state: FSMContext):
    await message.answer("Пожалуйста, введите Telegram ID вашего друга (он может узнать его, написав боту команду /id):")
    await state.set_state(QuizState.waiting_for_friend_id)

@dp.message(QuizState.waiting_for_friend_id)
async def process_friend_id(message: Message, state: FSMContext):
    friend_id_str = message.text.strip()
    if not friend_id_str.isdigit():
        await message.answer("ID должен состоять только из цифр. Попробуйте еще раз или напишите /start для выхода.")
        return
        
    try:
        with open('user_sessions_english.json', 'r', encoding='utf-8') as f:
            all_sessions = json.load(f)
    except:
        all_sessions = {}
        
    if friend_id_str not in all_sessions:
        await message.answer("Пользователь с таким ID не найден. Убедитесь, что друг хотя бы раз запускал этого бота.")
        return
        
    fsession = all_sessions[friend_id_str]
    f_total_db = len(questions)
    f_passed = len(fsession.get("answered", []))
    
    f_streak = fsession.get("streak", 0)
    f_xp = fsession.get("xp", 0)
    f_errors = len(fsession.get("errors", {}))
    
    friend_info_str = f"ID {friend_id_str}"
    try:
        f_chat = await bot.get_chat(friend_id_str)
        name_parts = []
        if f_chat.first_name:
            name_parts.append(f_chat.first_name.replace('_', '\\_').replace('*', '\\*').replace('[', '\\['))
        if f_chat.username:
            name_parts.append(f"(@{f_chat.username.replace('_', '\\_')})")
        if name_parts:
            friend_info_str = f"{' '.join(name_parts)} (ID {friend_id_str})"
    except Exception:
        pass
        
    stats_text = (
        f"📊 *Статистика друга ({friend_info_str}):*\n\n"
        f"🎯 *Прогресс базы:* {f_passed} / {f_total_db} вопросов\n"
        f"🔥 *Ударный режим (стрик):* {f_streak} дней\n"
        f"⚡ *Опыт (XP):* {f_xp}\n"
        f"🧠 *Сложных вопросов (SRS):* {f_errors}\n"
    )
    
    await message.answer(stats_text, parse_mode="Markdown")
    await state.clear()


@dp.message()
async def unknown_message(message: Message, state: FSMContext):
    st = await state.get_state()
    if st == QuizState.answering:
        await message.answer("Пожалуйста, используйте кнопки для ответа на вопрос! 👆", reply_markup=get_main_keyboard())
    else:
        await state.clear()
        await message.answer("Выбери действие из меню 👇", reply_markup=get_main_keyboard())


async def main():
    print("English Bot is starting...")
    
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Запуск бота"),
        BotCommand(command="set_date", description="Изменить дату экзамена"),
        BotCommand(command="reset", description="Сбросить прогресс"),
        BotCommand(command="admin", description="Админ-панель")
    ]
    await bot.set_my_commands(commands)
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
