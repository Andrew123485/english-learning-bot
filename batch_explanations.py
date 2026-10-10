"""
Batch pre-generation of DETAILED explanations for ALL 991 questions.
For each question generates:
  - Why the correct answer is correct
  - Why EACH wrong answer is wrong  
  - A short rule for notebook

Run: python batch_explanations.py
Can be interrupted and resumed — progress is saved after EVERY question.
Estimated time: ~2 hours with 2 API keys.
"""

import os
import json
import time
import sys
from dotenv import load_dotenv
from google import genai

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'questions_db.json')
OUTPUT_PATH = os.path.join(BASE_DIR, 'explanations_cache.json')

GEMINI_API_KEYS = [k.strip() for k in os.getenv('GEMINI_API_KEY', '').split(',') if k.strip()]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEY not found in .env!")

print(f"Using {len(GEMINI_API_KEYS)} API keys")

clients = [genai.Client(api_key=key) for key in GEMINI_API_KEYS]
cooldowns = [0.0] * len(clients)
current_key_idx = 0  # round-robin

# Load questions
with open(DB_PATH, 'r', encoding='utf-8') as f:
    questions = json.load(f)

# Load existing cache (for resume)
if os.path.exists(OUTPUT_PATH):
    with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    print(f"Resuming: {len(cache)} already generated")
else:
    cache = {}


def is_placeholder(text, q=None):
    """Check if explanation is a rate-limit placeholder or has bad formatting."""
    if not text:
        return True
    placeholders = ["_(ИИ временно", "_(Все ИИ", "_(Все доступные", 
                     "_(Ошибка ИИ", "*(Лимит запросов", "(Лимит запросов"]
    if any(text.strip().startswith(p) for p in placeholders):
        return True
    
    if q:
        if q['type'] == 'single' and "✅" not in text:
            return True
        if q['type'] == 'matching' and "🔗" not in text:
            return True
            
    return False


def build_prompt_single(q):
    """Build prompt for single-choice questions."""
    correct_answers = [a['text'] for a in q['answers'] if a.get('is_correct')]
    wrong_answers = [a['text'] for a in q['answers'] if not a.get('is_correct')]
    
    options_text = "\n".join([f"  {i+1}) {a['text']}" for i, a in enumerate(q['answers'])])
    correct_text = ", ".join(correct_answers)
    
    return f"""Ты — репетитор по английскому языку (уровень B2/C1, экзамен Verification).
Студент готовится к экзамену. Разбери это задание ПОДРОБНО на русском языке.

РАЗДЕЛ: {q['block_name']}

ЗАДАНИЕ:
{q['question']}

ВАРИАНТЫ:
{options_text}

ПРАВИЛЬНЫЙ ОТВЕТ: {correct_text}

ИНСТРУКЦИИ:
1. Объясни, почему правильный ответ верный (грамматическое правило, значение термина, контекст).
2. Объясни, почему КАЖДЫЙ неправильный вариант НЕ подходит (кратко, 1 предложение на каждый).
3. Дай ПРАВИЛО ДЛЯ БЛОКНОТА — 1-2 предложения, которые студент запишет и запомнит.

ФОРМАТ ОТВЕТА (строго соблюдай):
✅ {correct_text} — [почему верно, 1-2 предложения]

{chr(10).join([f'❌ {w} — [почему неверно, 1 предложение]' for w in wrong_answers])}

📝 Правило: [краткое правило для блокнота, 1-2 предложения]"""


def build_prompt_matching(q):
    """Build prompt for matching questions."""
    pairs_text = "\n".join([f"  {a['text']} → {a['match']}" for a in q['answers']])
    
    return f"""Ты — репетитор по английскому языку (уровень B2/C1, экзамен Verification).
Студент готовится к экзамену. Разбери это задание на лексику ПОДРОБНО на русском языке.

РАЗДЕЛ: {q['block_name']}

ЗАДАНИЕ:
{q['question']}

ПРАВИЛЬНЫЕ ПАРЫ:
{pairs_text}

ИНСТРУКЦИИ:
1. Объясни КАЖДУЮ пару: переведи термины и объясни, почему они подходят.
2. Приведи короткий пример использования каждого термина в контексте (на английском с переводом).
3. Дай ПРАВИЛО ДЛЯ БЛОКНОТА (лексический совет).

ФОРМАТ ОТВЕТА:
🔗 [термин] → [определение]
— [Перевод и подробное объяснение]
— Пример: [английский пример] — [перевод примера]

(повтори для каждой пары)

📝 Правило: [что запомнить, 1-2 предложения]"""


def generate_with_retry(prompt, max_retries=5):
    """Generate with round-robin key rotation and retry logic."""
    global current_key_idx
    
    for attempt in range(max_retries):
        # Find an available key
        now = time.time()
        
        # Try round-robin first
        tried = 0
        while tried < len(clients):
            idx = current_key_idx % len(clients)
            current_key_idx += 1
            tried += 1
            
            if cooldowns[idx] > now:
                continue
            
            try:
                response = clients[idx].models.generate_content(
                    model='gemini-2.0-flash',
                    contents=prompt
                )
                return response.text
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    cooldowns[idx] = time.time() + 65.0
                    print(f"    ⚠️  Key {idx} → 429, cooldown 65s")
                    continue
                else:
                    print(f"    ❌ Key {idx} error: {err[:80]}")
                    time.sleep(2)
                    continue
        
        # All keys on cooldown — wait for the earliest one
        earliest = min(cooldowns)
        wait = max(earliest - time.time() + 1, 5)
        print(f"    ⏳ All keys on cooldown, waiting {wait:.0f}s...")
        time.sleep(wait)
    
    return None


def save_cache():
    """Save cache to disk."""
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def main():
    total = len(questions)
    already = sum(1 for q in questions if str(q['id']) in cache and not is_placeholder(cache[str(q['id'])], q))
    to_do = total - already
    
    print(f"\n{'='*60}")
    print(f"  Всего вопросов:      {total}")
    print(f"  Уже есть:            {already}")
    print(f"  Нужно сгенерировать: {to_do}")
    print(f"  Примерное время:     {to_do * 5 // 60} мин")
    print(f"{'='*60}\n")
    
    if to_do == 0:
        print("✅ Все объяснения уже сгенерированы!")
        return
    
    generated = 0
    failed = 0
    start_time = time.time()
    
    for i, q in enumerate(questions):
        q_id = str(q['id'])
        
        # Skip if already generated (and not a placeholder)
        if q_id in cache and not is_placeholder(cache[q_id], q):
            continue
        
        # Build prompt based on question type
        if q['type'] == 'single':
            prompt = build_prompt_single(q)
        elif q['type'] == 'matching':
            prompt = build_prompt_matching(q)
        else:
            continue
        
        block_short = q['block_name'][:25]
        print(f"[{generated+1}/{to_do}] Q#{q_id} ({block_short})...", end=" ", flush=True)
        
        explanation = generate_with_retry(prompt)
        
        if explanation and not is_placeholder(explanation, q):
            cache[q_id] = explanation
            generated += 1
            
            # Save after EVERY question (safe to interrupt)
            save_cache()
            
            elapsed = time.time() - start_time
            rate = generated / elapsed * 60 if elapsed > 0 else 0
            remaining = (to_do - generated) / rate if rate > 0 else 0
            print(f"✅ ({rate:.0f}/мин, ~{remaining:.0f} мин осталось)")
        else:
            failed += 1
            print(f"❌ failed")
        
        # Delay between requests
        time.sleep(4)
    
    # Final save
    save_cache()
    
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  ГОТОВО!")
    print(f"  Сгенерировано: {generated}")
    print(f"  Ошибок:        {failed}")
    print(f"  Всего в кэше:  {len(cache)} / {total}")
    print(f"  Время:         {elapsed/60:.1f} мин")
    print(f"  Файл:          {OUTPUT_PATH}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
