"""
access_control.py — Единый модуль контроля доступа и монетизации для Telegram-ботов.
Подключается в bot.py через import. Хранит данные пользователей в JSON.
"""

import os
import json
from datetime import datetime, timedelta
from aiogram.types import Message, LabeledPrice, PreCheckoutQuery, ContentType
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram import Router, F

# ===== CONFIGURATION =====
ADMIN_ID = 576401462  # Твой Telegram ID
FREE_DAILY_LIMIT = 15  # Вопросов в день для Free
STAR_PRICE_MONTHLY = 200  # Stars за месяц подписки
STAR_PRICE_FOREVER = 350  # Stars за вечный доступ

# ===== USERS DATABASE =====
_users_db = {}
_users_db_path = None

def init_access_control(base_dir: str):
    """Initialize access control with the bot's base directory."""
    global _users_db, _users_db_path
    _users_db_path = os.path.join(base_dir, 'users_access.json')
    if os.path.exists(_users_db_path):
        with open(_users_db_path, 'r', encoding='utf-8') as f:
            _users_db = json.load(f)
    else:
        _users_db = {}

def _save_users():
    if _users_db_path:
        with open(_users_db_path, 'w', encoding='utf-8') as f:
            json.dump(_users_db, f, ensure_ascii=False, indent=2)

def _get_user(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in _users_db:
        _users_db[uid] = {
            "plan": "free",
            "paid_until": None,
            "paid_amount": 0,
            "registered": datetime.now().strftime("%Y-%m-%d"),
            "daily_free_used": 0,
            "last_free_date": "",
            "banned": False,
            "total_paid": 0
        }
        _save_users()
    return _users_db[uid]

def is_premium(user_id: int) -> bool:
    """Check if user has active premium."""
    if user_id == ADMIN_ID:
        return True
    user = _get_user(user_id)
    if user.get("plan") == "forever":
        return True
    if user.get("plan") == "premium" and user.get("paid_until"):
        return user["paid_until"] >= datetime.now().strftime("%Y-%m-%d")
    return False

def is_banned(user_id: int) -> bool:
    user = _get_user(user_id)
    return user.get("banned", False)

def check_free_limit(user_id: int) -> tuple[bool, int]:
    """
    Check if free user can answer more questions today.
    Returns (can_continue, remaining_count).
    """
    if is_premium(user_id):
        return True, 999
    
    user = _get_user(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Reset daily counter
    if user.get("last_free_date") != today:
        user["daily_free_used"] = 0
        user["last_free_date"] = today
        _save_users()
    
    global_config = _users_db.get("_config", {"global_limit": FREE_DAILY_LIMIT})
    limit = user.get("personal_limit", global_config.get("global_limit", FREE_DAILY_LIMIT))
    
    remaining = limit - user.get("daily_free_used", 0)
    return remaining > 0, max(0, remaining)

def increment_free_usage(user_id: int):
    """Increment the free usage counter after a question is served."""
    if is_premium(user_id):
        return
    user = _get_user(user_id)
    user["daily_free_used"] = user.get("daily_free_used", 0) + 1
    _save_users()

def activate_premium(user_id: int, plan: str, stars_paid: int):
    """Activate premium for a user after successful payment."""
    user = _get_user(user_id)
    user["total_paid"] = user.get("total_paid", 0) + stars_paid
    
    if plan == "forever":
        user["plan"] = "forever"
        user["paid_until"] = "9999-12-31"
        user["paid_amount"] = stars_paid
    else:  # monthly
        user["plan"] = "premium"
        if user.get("paid_until") and user["paid_until"] >= datetime.now().strftime("%Y-%m-%d"):
            # Extend existing subscription
            current_end = datetime.strptime(user["paid_until"], "%Y-%m-%d")
            user["paid_until"] = (current_end + timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            user["paid_until"] = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        user["paid_amount"] = stars_paid
    
    _save_users()

def ban_user(user_id: int):
    user = _get_user(user_id)
    user["banned"] = True
    _save_users()

def unban_user(user_id: int):
    user = _get_user(user_id)
    user["banned"] = False
    _save_users()

def grant_premium(user_id: int, plan: str = "forever"):
    """Admin grants premium manually."""
    activate_premium(user_id, plan, 0)

def get_stats() -> dict:
    """Get admin statistics."""
    total = len(_users_db)
    premium = sum(1 for u in _users_db.values() if u.get("plan") in ("premium", "forever"))
    total_revenue = sum(u.get("total_paid", 0) for u in _users_db.values())
    banned = sum(1 for u in _users_db.values() if u.get("banned"))
    global_limit = _users_db.get("_config", {}).get("global_limit", FREE_DAILY_LIMIT)
    return {
        "total_users": total - (1 if "_config" in _users_db else 0),
        "premium_users": premium,
        "free_users": total - premium - (1 if "_config" in _users_db else 0),
        "total_stars_earned": total_revenue,
        "banned_users": banned,
        "global_limit": global_limit
    }

# ===== PAYWALL MESSAGE =====
def get_paywall_text(remaining: int = 0) -> str:
    return (
        "🔒 *Для доступа требуется Premium-статус!*\n\n"
        "В бесплатной версии доступно ограничение на вопросы.\n\n"
        "⭐ *Premium-доступ* открывает:\n"
        "• Безлимитные вопросы каждый день\n"
        "• AI-объяснения к каждой ошибке\n"
        "• Индивидуальная система квизов (SRS)\n"
        "• Персональная Мега-Шпаргалка\n"
        "• Справочник Моя Грамматика\n"
        "• Режим экзамена и полная статистика\n\n"
        "Для перехода на тариф просто напиши: *@amorfatimementomoria*"
    )

def get_paywall_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Написать администратору", url="https://t.me/amorfatimementomoria")
    builder.adjust(1)
    return builder.as_markup()

# ===== ROUTER WITH PAYMENT HANDLERS =====
def setup_access_handlers(dp, bot_instance):
    """Register payment and admin handlers directly on the dispatcher."""
    
    @dp.callback_query(F.data == "buy_monthly")
    async def handle_buy_monthly(callback):
        await bot_instance.send_invoice(
            chat_id=callback.from_user.id,
            title="Premium подписка (1 месяц)",
            description="Безлимитные вопросы, AI-объяснения, SRS, экзамен",
            payload="premium_monthly",
            currency="XTR",
            prices=[LabeledPrice(label="Premium 1 мес", amount=STAR_PRICE_MONTHLY)],
        )
        await callback.answer()
    
    @dp.callback_query(F.data == "buy_forever")
    async def handle_buy_forever(callback):
        await bot_instance.send_invoice(
            chat_id=callback.from_user.id,
            title="Premium навсегда",
            description="Вечный безлимитный доступ ко всем функциям бота",
            payload="premium_forever",
            currency="XTR",
            prices=[LabeledPrice(label="Premium навсегда", amount=STAR_PRICE_FOREVER)],
        )
        await callback.answer()
    
    @dp.pre_checkout_query()
    async def handle_pre_checkout(pre_checkout: PreCheckoutQuery):
        await bot_instance.answer_pre_checkout_query(pre_checkout.id, ok=True)
    
    @dp.message(F.successful_payment)
    async def handle_successful_payment(message: Message):
        payload = message.successful_payment.invoice_payload
        stars = message.successful_payment.total_amount
        uid = message.from_user.id
        
        if payload == "premium_monthly":
            activate_premium(uid, "premium", stars)
            await message.answer(
                "🎉 *Premium активирован на 30 дней!*\n\n"
                "Теперь тебе доступны все функции без ограничений. Удачи на экзамене! 🚀",
                parse_mode="Markdown"
            )
        elif payload == "premium_forever":
            activate_premium(uid, "forever", stars)
            await message.answer(
                "💎 *Вечный Premium активирован!*\n\n"
                "Все функции разблокированы навсегда. Ты инвестировал в свои знания — это лучшее вложение! 🏆",
                parse_mode="Markdown"
            )
    
    @dp.message(F.text.startswith("/admin"))
    async def handle_admin(message: Message):
        if message.from_user.id != ADMIN_ID:
            await message.answer("⛔ Доступ запрещен.")
            return
        
        parts = message.text.split()
        
        # /admin — show stats
        if len(parts) == 1:
            stats = get_stats()
            await message.answer(
                f"📊 *Админ-панель*\n\n"
                f"👥 Всего пользователей: *{stats['total_users']}*\n"
                f"💎 Premium: *{stats['premium_users']}*\n"
                f"🆓 Free: *{stats['free_users']}*\n"
                f"⭐ Заработано Stars: *{stats['total_stars_earned']}*\n"
                f"🚫 Забанено: *{stats['banned_users']}*\n"
                f"⚙️ Глобальный лимит: *{stats['global_limit']}*",
                parse_mode="Markdown"
            )
        
        # /admin limit <number> or /admin limit <user_id> <number>
        elif len(parts) >= 3 and parts[1] == "limit":
            try:
                if len(parts) == 3: # Global limit
                    new_limit = int(parts[2])
                    if "_config" not in _users_db:
                        _users_db["_config"] = {}
                    _users_db["_config"]["global_limit"] = new_limit
                    _save_users()
                    await message.answer(f"✅ Глобальный лимит изменен на *{new_limit}* в день.", parse_mode="Markdown")
                elif len(parts) == 4: # Personal limit
                    target_id = parts[2]
                    new_limit = int(parts[3])
                    if target_id not in _users_db:
                        await message.answer("❌ Пользователь не найден в базе.")
                        return
                    _users_db[target_id]["personal_limit"] = new_limit
                    _save_users()
                    await message.answer(f"✅ Персональный лимит для `{target_id}` установлен на *{new_limit}*.", parse_mode="Markdown")
            except ValueError:
                await message.answer("❌ Ошибка. Используйте числа.")
        
        # /admin grant <user_id> — give premium
        elif len(parts) >= 3 and parts[1] == "grant":
            try:
                target_id = int(parts[2])
                grant_premium(target_id)
                await message.answer(f"✅ Premium навсегда выдан пользователю `{target_id}`", parse_mode="Markdown")
            except ValueError:
                await message.answer("❌ Неверный ID пользователя.")
        
        # /admin ban <user_id>
        elif len(parts) >= 3 and parts[1] == "ban":
            try:
                target_id = int(parts[2])
                ban_user(target_id)
                await message.answer(f"🚫 Пользователь `{target_id}` забанен.", parse_mode="Markdown")
            except ValueError:
                await message.answer("❌ Неверный ID пользователя.")
        
        # /admin unban <user_id>
        elif len(parts) >= 3 and parts[1] == "unban":
            try:
                target_id = int(parts[2])
                unban_user(target_id)
                await message.answer(f"✅ Пользователь `{target_id}` разбанен.", parse_mode="Markdown")
            except ValueError:
                await message.answer("❌ Неверный ID пользователя.")
        
        # /admin users — list all users
        elif len(parts) >= 2 and parts[1] == "users":
            lines = []
            for uid, u in _users_db.items():
                plan_icon = "💎" if u.get("plan") == "forever" else ("⭐" if u.get("plan") == "premium" else "🆓")
                ban_icon = " 🚫" if u.get("banned") else ""
                lines.append(f"{plan_icon} `{uid}` — {u.get('plan', 'free')}{ban_icon}")
            text = "👥 *Пользователи:*\n\n" + "\n".join(lines[:50]) if lines else "Пользователей нет."
            await message.answer(text, parse_mode="Markdown")
        
        else:
            await message.answer(
                "*Команды:*\n"
                "`/admin` — статистика\n"
                "`/admin users` — список юзеров\n"
                "`/admin grant <id>` — выдать Premium\n"
                "`/admin ban <id>` — забанить\n"
                "`/admin unban <id>` — разбанить",
                parse_mode="Markdown"
            )
    
    # End of setup
