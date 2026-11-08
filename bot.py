"""SubTrackBot — Telegram-бот для отслеживания платных подписок.

Возможности:
    /start    — приветствие и список команд
    /add      — диалог добавления подписки (название + дата окончания ДД.ММ.ГГГГ)
    /my_subs  — список подписок с остатком дней
    /remove   — удаление подписки через inline-кнопки
    /cancel   — отмена текущего диалога

Напоминания: задача JobQueue «daily_sub_check» раз в сутки проверяет базу
и за 3 дня до окончания подписки отправляет пользователю сообщение.

Запуск: BOT_TOKEN=<токен> python bot.py (подробнее — в README.md)
"""
import logging
import os
import re
from datetime import date, datetime, timedelta

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

# --------------------------------------------------------------------- настройки
REMIND_DAYS = 3                               # за сколько дней предупреждать
DATE_PATTERN = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

ASK_SERVICE, ASK_DATE = range(2)              # состояния диалога /add

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("subtrackbot")


# ----------------------------------------------------------------------- утилиты
def _fmt_date(iso_date: str) -> str:
    """'ГГГГ-ММ-ДД' (формат БД) -> 'ДД.ММ.ГГГГ' (формат пользователя)."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")


def _parse_user_date(text: str):
    """Разбирает строку 'ДД.ММ.ГГГГ'. Возвращает date или None при ошибке."""
    text = text.strip()
    if not DATE_PATTERN.match(text):
        return None
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        # например 31.02.2026 — формат верный, но такой даты не существует
        return None


def _days_left(end_date_iso: str) -> int:
    """Сколько дней осталось до окончания (может быть отрицательным)."""
    end = datetime.strptime(end_date_iso, "%Y-%m-%d").date()
    return (end - date.today()).days


# ----------------------------------------------------------------------- команды
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие: что умеет бот."""
    await update.message.reply_text(
        "👋 Привет! Я SubTrackBot — слежу за сроками ваших платных подписок "
        "(Netflix, Spotify, YouTube Premium и любые другие).\n\n"
        "Добавьте подписку — и я напомню за 3 дня до её окончания.\n\n"
        "Команды:\n"
        "/add — добавить подписку\n"
        "/my_subs — список активных подписок\n"
        "/remove — удалить подписку\n"
        "/cancel — отменить текущее действие"
    )


# ------------------------------------------------------------------ диалог /add
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа: просим название сервиса."""
    await update.message.reply_text(
        "Введите название сервиса (например: Netflix).\n"
        "Для отмены — /cancel"
    )
    return ASK_SERVICE


async def add_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимаем название и просим дату."""
    name = (update.message.text or "").strip()
    if not name or len(name) > 64:
        await update.message.reply_text(
            "Название не может быть пустым или длиннее 64 символов. Попробуйте ещё раз:"
        )
        return ASK_SERVICE
    context.user_data["new_service"] = name
    await update.message.reply_text(
        f"Записал: «{name}».\n"
        "Теперь введите дату окончания в формате ДД.ММ.ГГГГ (например, 15.12.2026):"
    )
    return ASK_DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Проверяем дату и сохраняем подписку в базу."""
    parsed = _parse_user_date(update.message.text or "")
    if parsed is None:
        await update.message.reply_text(
            "Не понял дату 😕 Нужен формат ДД.ММ.ГГГГ, например 15.12.2026.\n"
            "Проверьте, что день не больше 31, месяц не больше 12 и такая дата существует."
        )
        return ASK_DATE
    if parsed < date.today():
        await update.message.reply_text(
            "Эта дата уже в прошлом. Введите дату окончания, которая ещё не наступила:"
        )
        return ASK_DATE

    service = context.user_data.pop("new_service", "Подписка")
    try:
        updated = db.add_subscription(update.effective_user.id, service, parsed.isoformat())
    except Exception:
        logger.exception("Не удалось сохранить подписку в базу")
        await update.message.reply_text(
            "Произошла ошибка базы данных. Попробуйте позже или повторите /add."
        )
        return ConversationHandler.END

    until = parsed.strftime("%d.%m.%Y")
    if updated:
        await update.message.reply_text(
            f"Обновил подписку «{service}» — теперь до {until}."
        )
    else:
        await update.message.reply_text(
            f"✅ Готово! Подписка «{service}» сохранена — до {until}.\n"
            f"Напомню за {REMIND_DAYS} дня до окончания."
        )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога."""
    context.user_data.pop("new_service", None)
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


# ----------------------------------------------------------------------- /my_subs
async def my_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список подписок пользователя с остатком дней."""
    try:
        subs = db.get_user_subscriptions(update.effective_user.id)
    except Exception:
        logger.exception("Не удалось прочитать подписки из базы")
        await update.message.reply_text("Произошла ошибка базы данных. Попробуйте позже.")
        return

    if not subs:
        await update.message.reply_text("Активных подписок нет. Добавьте первую: /add")
        return

    lines = []
    for sub in subs:
        days = _days_left(sub["end_date"])
        if days > 0:
            note = f"осталось {days} дн."
        elif days == 0:
            note = "истекает сегодня"
        else:
            note = "уже истекла"
        lines.append(f"• {sub['service_name']} — до {_fmt_date(sub['end_date'])} ({note})")
    await update.message.reply_text("📋 Ваши подписки:\n" + "\n".join(lines))


# ----------------------------------------------------------------------- /remove
async def remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показываем список подписок в виде кнопок."""
    try:
        subs = db.get_user_subscriptions(update.effective_user.id)
    except Exception:
        logger.exception("Не удалось прочитать подписки из базы")
        await update.message.reply_text("Произошла ошибка базы данных. Попробуйте позже.")
        return

    if not subs:
        await update.message.reply_text("Удалять нечего — подписок пока нет. Добавить: /add")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"🗑 {sub['service_name']} — до {_fmt_date(sub['end_date'])}",
            callback_data=f"del:{sub['id']}",
        )]
        for sub in subs
    ]
    await update.message.reply_text(
        "Какую подписку удалить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def remove_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на кнопку — удаляем выбранную запись."""
    query = update.callback_query
    await query.answer()

    try:
        sub_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("Не удалось распознать выбор. Попробуйте /remove ещё раз.")
        return

    try:
        deleted = db.delete_subscription(sub_id, update.effective_user.id)
    except Exception:
        logger.exception("Не удалось удалить подписку id=%s", sub_id)
        await query.edit_message_text("Произошла ошибка базы данных. Попробуйте позже.")
        return

    if deleted:
        await query.edit_message_text("🗑 Подписка удалена.")
    else:
        # чужую (или уже удалённую) запись удалить не выйдет: проверяется user_id
        await query.edit_message_text("Эта подписка уже удалена или не найдена.")


# --------------------------------------------------------------- ежедневная задача
async def check_subscriptions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная проверка: напомнить о подписках, истекающих через REMIND_DAYS дней.

    Ошибки внутри задачи обёрнуты в try/except — сбой останавливает
    только текущий запуск задачи, но не весь бот.
    """
    if context.job is not None:
        logger.info("Запуск задачи '%s', data=%s", context.job.name, context.job.data)

    try:
        subs = db.get_all_subscriptions()
    except Exception:
        logger.exception("Задача напоминаний: не удалось прочитать базу")
        return

    for sub in subs:
        try:
            days = _days_left(sub["end_date"])
        except (ValueError, TypeError):
            logger.warning("Некорректная дата в базе: %r (id=%s)", sub["end_date"], sub["id"])
            continue

        if days != REMIND_DAYS or sub["reminded"]:
            continue

        try:
            await context.bot.send_message(
                chat_id=sub["user_id"],
                text=f"🔔 Ваша подписка на {sub['service_name']} закончится через {REMIND_DAYS} дня!",
            )
            db.mark_reminded(sub["id"])       # чтобы не напоминать повторно
        except Exception:
            # пользователь мог заблокировать бота, пропасть сеть и т.п.
            logger.exception("Не удалось отправить напоминание пользователю %s", sub["user_id"])


# ------------------------------------------------------------- обработчик ошибок
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный перехват ошибок — бот не падает, а пишет в лог."""
    logger.error("Необработанная ошибка:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Что-то пошло не так 😔 Попробуйте ещё раз позже."
            )
        except Exception:
            logger.exception("Не удалось сообщить пользователю об ошибке")


# ----------------------------------------------------------------------- запуск
def check_token(token: str):
    """Проверка токена через Telegram API (getMe) перед стартом.

    Возвращает True/False; None — сеть недоступна, результат неизвестен.
    """
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        return resp.json().get("ok") is True
    except (requests.RequestException, ValueError):
        return None


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения BOT_TOKEN "
            "(инструкция — в README.md)."
        )

    status = check_token(token)
    if status is False:
        raise SystemExit("Telegram отклонил токен — он неверный или отозван. Проверьте BOT_TOKEN.")
    if status is None:
        logger.warning("Не удалось проверить токен (нет сети?). Пробую запустить бота.")

    db.init_db()

    application = Application.builder().token(token).build()

    # диалог добавления подписки
    add_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ASK_SERVICE: [MessageHandler(~filters.COMMAND, add_service)],
            ASK_DATE: [MessageHandler(~filters.COMMAND, add_date)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # выбор подписки для удаления
    remove_handler = CommandHandler("remove", remove_start)

    # Регистрация обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(add_handler)
    application.add_handler(remove_handler)
    application.add_handler(CallbackQueryHandler(remove_button, pattern=r"^del:\d+$"))
    application.add_handler(CommandHandler("my_subs", my_subs))
    application.add_error_handler(error_handler)

    # Настройка JobQueue — ежедневная проверка сроков
    job_queue = application.job_queue
    if job_queue is None:
        logger.error(
            "JobQueue недоступен: напоминания работать не будут. "
            'Установите пакет с extras: pip install "python-telegram-bot[job-queue]"'
        )
    else:
        job = job_queue.run_repeating(
            check_subscriptions,           # функция-задача
            interval=timedelta(days=1),    # каждые 24 ч
            first=10,                      # первая проверка через 10 с
            name="daily_sub_check",
            data={},
        )
        # ссылку на job можно сохранить, чтобы позже отменить задачу:
        # job.schedule_removal()
        # Альтернатива — проверка в фиксированное время:
        # from datetime import time as dtime
        # job_queue.run_daily(check_subscriptions, time=dtime(hour=3, minute=0))
        logger.info("Задача '%s' запланирована", job.name)

    logger.info("Бот запущен, ожидаем сообщения…")
    application.run_polling()


if __name__ == "__main__":
    main()
