"""Слой данных SubTrackBot: локальная SQLite-база подписок.

Таблица subscriptions:
    id           — PK, нужен для выбора конкретной записи в /remove
    user_id      — Telegram id владельца
    service_name — название сервиса (Netflix, Spotify, ...)
    end_date     — дата окончания, строка ISO 'ГГГГ-ММ-ДД'
    reminded     — 1, если напоминание за 3 дня уже отправлено
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).with_name("subscriptions.db")


@contextmanager
def _connect():
    """Соединение с авто-commit и гарантированным закрытием."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Создаёт таблицу и индекс, если их ещё нет."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                service_name TEXT    NOT NULL,
                end_date     TEXT    NOT NULL,
                reminded     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions (user_id)"
        )


def add_subscription(user_id: int, service_name: str, end_date_iso: str) -> bool:
    """Добавляет подписку. Повторное добавление того же сервиса обновляет дату.

    Возвращает True, если была обновлена существующая запись.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM subscriptions "
            "WHERE user_id = ? AND lower(service_name) = lower(?)",
            (user_id, service_name),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE subscriptions SET end_date = ?, reminded = 0 WHERE id = ?",
                (end_date_iso, row["id"]),
            )
            return True
        conn.execute(
            "INSERT INTO subscriptions (user_id, service_name, end_date) VALUES (?, ?, ?)",
            (user_id, service_name, end_date_iso),
        )
        return False


def get_user_subscriptions(user_id: int) -> list:
    """Подписки пользователя, отсортированные по дате окончания."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, service_name, end_date FROM subscriptions "
            "WHERE user_id = ? ORDER BY end_date",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_all_subscriptions() -> list:
    """Все подписки всех пользователей — для ежедневной задачи напоминаний."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, service_name, end_date, reminded FROM subscriptions"
        ).fetchall()
        return [dict(row) for row in rows]


def delete_subscription(sub_id: int, user_id: int) -> bool:
    """Удаляет подписку (только свою). Возвращает True, если удалена."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE id = ? AND user_id = ?",
            (sub_id, user_id),
        )
        return cur.rowcount > 0


def mark_reminded(sub_id: int) -> None:
    """Отмечает, что напоминание уже отправлено."""
    with _connect() as conn:
        conn.execute("UPDATE subscriptions SET reminded = 1 WHERE id = ?", (sub_id,))
