import csv
import logging
import os
import sys
import re
from typing import Dict, Any, Optional
from collections import defaultdict, Counter
from dotenv import load_dotenv
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Загрузка переменных окружения
load_dotenv()

# Функция для маскировки чувствительных данных в логах
def mask_sensitive_data(message):
    if not isinstance(message, str):
        return message
    # Маскируем BOT_TOKEN
    message = re.sub(r'(BOT_TOKEN[\s=:]+)([^\s]+)', r'\1***', message, flags=re.IGNORECASE)
    return message

# Кастомный форматтер логов с маскировкой
class SafeLogFormatter(logging.Formatter):
    def format(self, record):
        original = super().format(record)
        return mask_sensitive_data(original)

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
file_handler = logging.FileHandler('succ_bot.log')
formatter = SafeLogFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.addHandler(file_handler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


class UserSession:
    __slots__ = ['branch', 'current_q', 'advices', 'confirmations', 'history', 'portraits', 'seen_subscription_prompt']
    def __init__(self):
        self.branch: Optional[int] = None
        self.current_q: Optional[int] = None
        self.advices: list = []
        self.confirmations: list = []
        self.history: list = []
        self.portraits: list = []
        self.seen_subscription_prompt: bool = False  # Чтобы не показывать подписку дважды

    def start_branch(self, branch: int):
        self.branch = branch
        self.current_q = 1
        self.advices.clear()
        self.confirmations.clear()
        self.history = [1]
        self.portraits.clear()
        self.seen_subscription_prompt = False

    @property
    def portrait(self) -> str:
        if self.portraits:
            return Counter(self.portraits).most_common(1)[0][0]
        return "универсальный работник"

    def add_advice(self, advice: str):
        if advice and advice.strip():
            self.advices.append(advice.strip())

    def add_confirmation(self, confirmation: str):
        if confirmation and confirmation.strip():
            self.confirmations.append(confirmation.strip())

    def add_portrait(self, portrait: str):
        if portrait and portrait.strip():
            self.portraits.append(portrait.strip())

    def get_current_question(self, questions: dict) -> Optional[dict]:
        if self.branch is None or self.current_q is None:
            return None
        return questions.get(self.branch, {}).get(self.current_q)

    def move_to_next(self, next_q: int):
        if next_q is not None:
            self.current_q = next_q
            self.history.append(next_q)

    def go_back(self) -> bool:
        if len(self.history) > 1:
            self.history.pop()
            self.current_q = self.history[-1]
            return True
        return False


class FinanceBot:
    def __init__(self):
        self.images_dir = "images"
        if not os.path.exists(self.images_dir):
            os.makedirs(self.images_dir)
        self.user_sessions: Dict[int, UserSession] = {}
        self.questions = self.load_questions()
        self.texts = self.load_texts()
        self.community_link = os.getenv("COMMUNITY_LINK", "https://t.me/+25yK94v9nCoyNzFi")
        self.rss_feed_url = "https://fetchrss.com/feed/aI7uY390SFnyaI7uRt1OAptT.rss"

    def _clean_title(self, title: str) -> str:
        title = ' '.join(title.split())
        match = re.match(r'^([^.]*)\.', title)
        if match:
            cleaned = match.group(1).strip()
            if cleaned:
                return cleaned
        words = title.split()
        if len(words) > 6:
            return ' '.join(words[:6]) + '...'
        return title

    async def get_channel_updates(self) -> str:
        """Получаем последние 5 постов из RSS фида канала"""
        if not self.rss_feed_url:
            logger.warning("RSS_FEED_URL не указан в .env")
            return "Не удалось загрузить обновления."
        try:
            feed = feedparser.parse(self.rss_feed_url)
            if feed.bozo and not feed.entries:
                logger.warning("RSS не распознан: %s", feed.bozo_exception)
                return "Нет доступных материалов."
            seen = set()
            updates = []
            for i, entry in enumerate(feed.entries[:5]):
                clean_title = self._clean_title(entry.title)
                link = entry.link
                if link in seen:
                    continue
                updates.append(f"{i+1}. <a href='{link}'>{clean_title}</a>")
                seen.add(link)
            return "\n".join(updates) if updates else "Нет новых материалов."
        except Exception as e:
            logger.error("Ошибка при получении RSS: %s", mask_sensitive_data(str(e)))
            return "Не удалось загрузить обновления."

    def load_texts(self) -> Dict[str, str]:
        texts = {}
        try:
            csv_path = os.path.join(os.path.dirname(__file__), "texts.csv")
            if not os.path.exists(csv_path):
                logger.error("Файл texts.csv не найден по пути: %s", csv_path)
                return texts
            with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row.get("key") or not row.get("text"):
                        continue
                    texts[row["key"]] = row["text"]
        except Exception as e:
            logger.error("Ошибка загрузки texts.csv: %s", mask_sensitive_data(str(e)))
        return texts

    def load_questions(self) -> Dict[int, Dict[int, dict]]:
        questions = defaultdict(dict)
        csv_path = os.path.join(os.path.dirname(__file__), "questions_succ.csv")
        if not os.path.exists(csv_path):
            logger.error("Файл вопросов %s не найден", csv_path)
            return questions
        try:
            with open(csv_path, mode='r', encoding='utf-8-sig') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    try:
                        if not row.get("Ветка") or not row.get("Номер вопроса"):
                            continue
                        branch = int(row["Ветка"])
                        q_id = int(row["Номер вопроса"])
                        if q_id not in questions[branch]:
                            image_path = os.path.join(self.images_dir, f"image{q_id}.jpg")
                            questions[branch][q_id] = {
                                "text": row.get("Вводная", ""),
                                "options": {},
                                "is_final": row.get("Финал", "").strip().lower() in ("да", "yes", "1"),
                                "image_path": image_path if os.path.exists(image_path) else None
                            }
                        if row.get("Выбор пользователя") and row.get("Вариант вопроса"):
                            choice = int(row["Выбор пользователя"])
                            questions[branch][q_id]["options"][choice] = {
                                "text": row["Вариант вопроса"],
                                "next_q": int(row["Следующий вопрос"]) if row.get("Следующий вопрос") else None,
                                "confirmation": row.get("Подтверждение выбора", "").strip(),
                                "emoji": row.get("Эмодзи", "🔹"),
                                "portrait": row.get("Портрет", "универсальный работник"),
                                "advice": row.get("Совет", ""),
                                "description": row.get("Описание портрета", "")
                            }
                    except (ValueError, KeyError) as e:
                        logger.error("Ошибка обработки строки CSV: %s. Ошибка: %s",
                                     mask_sensitive_data(str(row)), mask_sensitive_data(str(e)))
                        continue
        except Exception as e:
            logger.error("Ошибка загрузки CSV: %s", mask_sensitive_data(str(e)))
        return questions

    async def ask_for_subscription(self, user_id: int, query: CallbackQuery):
        session = self.user_sessions.get(user_id)
        if not session:
            return

        if session.seen_subscription_prompt:
            await self.show_final_message(user_id, query)
            return

        session.seen_subscription_prompt = True

        text = (
            "🎉 <b>Поздравляем, вы успешно завершили тест!</b>\n\n"
            "Перед тем, как получить финальный результат, предлагаем подписаться на канал <b>Коллектиум</b>.\n\n"
            "Площадка для умных и любознательных людей, с авторской аналитикой всех значимых событий в мире. "
            "Новости политики, экономики и технологий. Главные тренды и ключевые игроки — всё, что нужно знать "
            "о тайных механизмах нашей цивилизации."
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подписаться", url="https://t.me/day_capitalist")],
            [InlineKeyboardButton("➡️ Пропустить", callback_data="skip_subscription")]
        ])

        try:
            await query.edit_message_text(
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            if "message is not modified" in str(e) or "not enough rights" in str(e):
                pass
            else:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                try:
                    await query.message.reply_text(
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception as e2:
                    logger.error("Не удалось отправить подписку: %s", mask_sensitive_data(str(e2)))
                    await self.show_final_message(user_id, query)

    async def skip_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        try:
            await query.message.delete()
        except Exception:
            pass
        await self.show_final_message(user_id, query)

    async def show_final_message(self, user_id: int, query: CallbackQuery):
        session = self.user_sessions.get(user_id)
        if not session:
            return

        portrait_key = session.portrait.lower()
        portrait_description = ""
        for branch in self.questions.values():
            for question in branch.values():
                for option in question.get("options", {}).values():
                    if option.get("portrait", "").lower() == portrait_key:
                        desc = option.get("description", "").strip()
                        if desc:
                            portrait_description = desc
                            break
                if portrait_description:
                    break
            if portrait_description:
                break

        if not portrait_description:
            portrait_description = (
                f"<b>Твой профессиональный портрет: {session.portrait}</b>\n"
                "Ты обладаешь достаточным сочетанием разумных качеств, которые помогут тебе последовательно добиться успеха в карьере."
            )

        unique_advices = list(dict.fromkeys(session.advices))
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        advice_lines = []
        for i, advice in enumerate(unique_advices):
            formatted_advice = advice.replace('*', '')
            dot_pos = formatted_advice.find('.')
            newline_pos = formatted_advice.find('\n')
            split_pos = -1
            if dot_pos > 0 and newline_pos > 0:
                split_pos = min(dot_pos, newline_pos)
            elif dot_pos > 0:
                split_pos = dot_pos
            elif newline_pos > 0:
                split_pos = newline_pos
            if split_pos > 0:
                portrait_name = formatted_advice[:split_pos].strip()
                advice_text = formatted_advice[split_pos+1:].strip()
                if formatted_advice[split_pos] == '.':
                    portrait_name += '.'
                advice_lines.append(f"{number_emojis[i] if i < len(number_emojis) else f'{i+1}.'} <b>{portrait_name}</b>\n{advice_text}")
            else:
                advice_lines.append(f"{number_emojis[i] if i < len(number_emojis) else f'{i+1}.'} {formatted_advice}")

        channel_updates = await self.get_channel_updates()

        salary_template_link = "https://docs.google.com/document/d/1hOaWvUnRAfpb0Gf4yo6Xp49lFmCQ2oCsaxKMyVSyVt8/edit?tab=t.0"

        final_text = (
            f"{portrait_description}\n"
            f"🎯 <b>Твои персональные рекомендации:</b>\n"
            + "\n".join(advice_lines) + "\n"
            f"\n""📌 <b>Бонус:</b> Уверен в своей ценности? Используй шаблон письменного заявления на повышение зарплаты:\n"
            f"<a href='{salary_template_link}'>📄 Открыть в GoogleDoc </a>\n\n"
            "<b>Не замыкайся только в работе. Если хочешь повысить свой уровень по жизни, следи за всеми трендами.</b>\n\n"
            f"Подпишись на <b>Коллектиум</b> — авторский канал о финансах, технологиях, "
            f"экономике и геополитике. Узнай, как устроен наш мир и куда он движется!\n\n"
            f"<b>Последние материалы:</b>\n"
            f"{channel_updates}\n"
            f"\n""Присоединяйся: <a href='https://t.me/day_capitalist'>Канал</a> | <a href='https://t.me/day_capitalist_club'>Сообщество</a>"
        )

        try:
            try:
                await query.edit_message_text(
                    text=final_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]]),
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception:
                await query.message.reply_text(
                    text=final_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]]),
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                try:
                    await query.message.delete()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Критическая ошибка при показе финального сообщения: %s", mask_sensitive_data(str(e)))
            await query.message.reply_text(
                "Произошла ошибка при формировании результатов. Пожалуйста, попробуйте снова.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Начать заново", callback_data="restart")]])
            )
        finally:
            self.user_sessions.pop(user_id, None)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self.user_sessions[user_id] = UserSession()
        message = update.message if update.message else update.callback_query.message
        start_image_path = os.path.join(self.images_dir, "image0.jpg")
        caption = (
            "👋 <b>Добро пожаловать в карьерного советника!</b>\n"
            "Этот бот поможет тебе:\n"
            "- Определить твой профиль\n"
            "- Дать персонализированные рекомендации\n"
            "Готов начать? Нажми кнопку ниже!"
        )
        try:
            if os.path.exists(start_image_path):
                with open(start_image_path, 'rb') as photo:
                    await message.reply_photo(
                        photo=photo,
                        caption=caption,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚀 Начать опрос", callback_data="branch_1")]
                        ]),
                        parse_mode="HTML"
                    )
            else:
                await message.reply_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🚀 Начать опрос", callback_data="branch_1")]
                    ]),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error("Ошибка в команде start: %s", mask_sensitive_data(str(e)))
            await message.reply_text("Произошла ошибка. Пожалуйста, попробуйте позже.")

    async def handle_branch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        try:
            branch = int(query.data.split("_")[1])
            self.user_sessions[user_id] = UserSession()
            session = self.user_sessions[user_id]
            session.start_branch(branch)
            if branch == 1:
                session.current_q = 2
                session.history = [1, 2]
            question = session.get_current_question(self.questions)
            if not question:
                await self.clean_session(user_id, update, "Ошибка: вопрос не найден")
                return
            text = question['text']
            if session.confirmations:
                text = "✅ " + "\n".join(session.confirmations) + "\n" + text
                session.confirmations.clear()
            keyboard = [
                [InlineKeyboardButton(f"{opt.get('emoji', '🔹')} {opt['text']}", callback_data=f"answer_{cid}")]
                for cid, opt in question["options"].items()
            ]
            if len(session.history) > 1:
                keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
            try:
                if question.get("image_path"):
                    try:
                        with open(question["image_path"], 'rb') as photo:
                            await query.message.reply_photo(
                                photo=photo,
                                caption=text,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode="Markdown"
                            )
                    except FileNotFoundError:
                        logger.warning(f"Image not found: {question['image_path']}")
                        await query.edit_message_text(
                            text=text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
                else:
                    await query.edit_message_text(
                        text=text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error("Ошибка показа вопроса: %s", mask_sensitive_data(str(e)))
                await self.clean_session(user_id, update, "Ошибка при отображении вопроса.")
        except Exception as e:
            logger.error("Ошибка в handle_branch: %s", mask_sensitive_data(str(e)))
            await self.clean_session(user_id, update, "Произошла ошибка. Давайте начнём заново.")

    async def show_question(self, update: Update, user_id: int):
        session = self.user_sessions.get(user_id)
        if not session:
            await self.clean_session(user_id, update)
            return
        question = session.get_current_question(self.questions)
        if not question:
            await self.clean_session(user_id, update, "Ошибка: вопрос не найден")
            return
        text = question['text']
        if session.confirmations:
            text = "✅ " + "\n".join(session.confirmations) + "\n" + text
            session.confirmations.clear()
        keyboard = [
            [InlineKeyboardButton(f"{opt.get('emoji', '🔹')} {opt['text']}", callback_data=f"answer_{cid}")]
            for cid, opt in question["options"].items()
        ]
        if len(session.history) > 1:
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
        try:
            if question.get("image_path"):
                try:
                    with open(question["image_path"], 'rb') as photo:
                        if update.callback_query:
                            await update.callback_query.message.reply_photo(
                                photo=photo,
                                caption=text,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode="Markdown"
                            )
                        else:
                            await update.message.reply_photo(
                                photo=photo,
                                caption=text,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode="Markdown"
                            )
                except FileNotFoundError:
                    logger.warning(f"Image not found: {question['image_path']}")
                    if update.callback_query:
                        await update.callback_query.edit_message_text(
                            text=text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
                    else:
                        await update.message.reply_text(
                            text=text,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
            else:
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text=text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(
                        text=text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="Markdown"
                    )
        except Exception as e:
            logger.error("Ошибка показа вопроса: %s", mask_sensitive_data(str(e)))
            await self.clean_session(user_id, update, "Ошибка при отображении вопроса.")

    async def handle_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query: CallbackQuery = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id)
        if not session:
            await self.clean_session(user_id, update)
            return
        try:
            choice_id = int(query.data.split("_")[1])
            question = session.get_current_question(self.questions)
            if not question:
                await self.clean_session(user_id, update, "Ошибка: вопрос не найден")
                return
            option = question["options"].get(choice_id)
            if not option:
                await query.message.reply_text("Неверный выбор")
                return
            if option.get("confirmation"):
                session.add_confirmation(option["confirmation"])
            if option.get("portrait"):
                session.add_portrait(option["portrait"])
            if option.get("advice"):
                session.add_advice(option["advice"])
            next_q = option.get("next_q")
            if next_q is None or question.get("is_final", False) or (session.branch == 1 and session.current_q == 12):
                await self.ask_for_subscription(user_id, query)
                return
            session.move_to_next(next_q)
            await self.show_question(update, user_id)
        except Exception as e:
            logger.error("Ошибка обработки ответа: %s", mask_sensitive_data(str(e)))
            await self.clean_session(user_id, update, "Произошла ошибка при обработке ответа.")

    async def handle_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id)
        if not session or not session.go_back():
            await query.message.reply_text("Нельзя вернуться назад")
            return
        await self.show_question(update, user_id)

    async def handle_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        self.user_sessions[user_id] = UserSession()
        await self.start(update, context)

    async def clean_session(self, user_id: int, update: Update, msg: str = "Сессия сброшена"):
        self.user_sessions.pop(user_id, None)
        try:
            if update.callback_query:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
        except Exception as e:
            logger.error("Ошибка при очистке сессии: %s", mask_sensitive_data(str(e)))

    def run(self):
        token = os.getenv("BOT_TOKEN")
        if not token:
            logger.error("BOT_TOKEN не найден в .env файле")
            return
        try:
            app = Application.builder().token(token).build()
            app.add_handler(CommandHandler("start", self.start))
            app.add_handler(CallbackQueryHandler(self.handle_branch, pattern=r"^branch_"))
            app.add_handler(CallbackQueryHandler(self.handle_restart, pattern=r"^restart$"))
            app.add_handler(CallbackQueryHandler(self.handle_back, pattern=r"^back$"))
            app.add_handler(CallbackQueryHandler(self.handle_answer, pattern=r"^answer_"))
            app.add_handler(CallbackQueryHandler(self.skip_subscription, pattern=r"^skip_subscription$"))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         lambda u, c: u.message.reply_text("Пожалуйста, используйте кнопки для навигации")))
            logger.info("Финансовый бот запущен")
            app.run_polling()
        except KeyboardInterrupt:
            logger.info("Бот остановлен вручную")
        except Exception as e:
            logger.error("Ошибка при запуске бота: %s", mask_sensitive_data(str(e)))


if __name__ == "__main__":
    try:
        bot = FinanceBot()
        bot.run()
    except Exception as e:
        logger.error("Критическая ошибка: %s", mask_sensitive_data(str(e)))
        input("Нажмите Enter для выхода...")