import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATA_DIR, DEFAULT_SETTINGS, SESSIONS_DIR, UPLOADS_DIR, settings
from database.models import Base, Setting

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

_DB_URL = settings.async_database_url
if "sqlite" in _DB_URL.lower():
    raise RuntimeError("SQLite отключён. Нужен PostgreSQL.")
_ENGINE_KW: dict = {"echo": False, "pool_pre_ping": True, "pool_size": 5, "max_overflow": 10}

engine = create_async_engine(_DB_URL, **_ENGINE_KW)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

_init_lock = asyncio.Lock()
_initialized = False

_USER_COLUMNS = {
    "pending_service": "ALTER TABLE users ADD COLUMN pending_service VARCHAR(32) DEFAULT ''",
    "pending_service_payload": "ALTER TABLE users ADD COLUMN pending_service_payload TEXT DEFAULT '{}'",
    "task_cycle_index": "ALTER TABLE users ADD COLUMN task_cycle_index INTEGER DEFAULT 0",
    "pending_contest_id": "ALTER TABLE users ADD COLUMN pending_contest_id INTEGER",
    "pending_contest_payload": "ALTER TABLE users ADD COLUMN pending_contest_payload TEXT DEFAULT '{}'",
    "hour_remind_sent": "ALTER TABLE users ADD COLUMN hour_remind_sent BOOLEAN DEFAULT TRUE",
    "pending_greeting_id": "ALTER TABLE users ADD COLUMN pending_greeting_id INTEGER",
}

_POST_COLUMNS = {
    "extra_buttons": "ALTER TABLE {table} ADD COLUMN extra_buttons TEXT DEFAULT '[]'",
    "copy_chat_id": "ALTER TABLE {table} ADD COLUMN copy_chat_id BIGINT",
    "copy_message_id": "ALTER TABLE {table} ADD COLUMN copy_message_id INTEGER",
}

_CONTEST_COLUMNS = {
    "winners_count": "ALTER TABLE contests ADD COLUMN winners_count INTEGER DEFAULT 1",
    "winner_ids": "ALTER TABLE contests ADD COLUMN winner_ids TEXT DEFAULT '[]'",
    "sponsor_kind": "ALTER TABLE contests ADD COLUMN sponsor_kind VARCHAR(16) DEFAULT 'service'",
    "own_sponsors": "ALTER TABLE contests ADD COLUMN own_sponsors TEXT DEFAULT '[]'",
    "source": "ALTER TABLE contests ADD COLUMN source VARCHAR(16) DEFAULT 'manual'",
}

_GREET_COLUMNS = {
    "post_delay_seconds": "ALTER TABLE channel_greetings ADD COLUMN post_delay_seconds INTEGER DEFAULT 5",
}

_GREET_POST_COLUMNS = {
    "delay_after_seconds": "ALTER TABLE greeting_posts ADD COLUMN delay_after_seconds INTEGER DEFAULT 5",
}

_COMBO_COLUMNS = {
    "cycle_seconds": "ALTER TABLE auto_combos ADD COLUMN cycle_seconds INTEGER DEFAULT 36000",
}

_COMBO_POST_COLUMNS = {
    "kind": "ALTER TABLE auto_combo_posts ADD COLUMN kind VARCHAR(16) DEFAULT 'bait'",
    "offset_seconds": "ALTER TABLE auto_combo_posts ADD COLUMN offset_seconds INTEGER DEFAULT 0",
    "contest_text": "ALTER TABLE auto_combo_posts ADD COLUMN contest_text TEXT DEFAULT ''",
    "contest_parse_mode": "ALTER TABLE auto_combo_posts ADD COLUMN contest_parse_mode VARCHAR(16) DEFAULT 'HTML'",
    "contest_media_type": "ALTER TABLE auto_combo_posts ADD COLUMN contest_media_type VARCHAR(16) DEFAULT 'photo'",
    "contest_media_file_id": "ALTER TABLE auto_combo_posts ADD COLUMN contest_media_file_id VARCHAR(256) DEFAULT ''",
    "contest_button_text": "ALTER TABLE auto_combo_posts ADD COLUMN contest_button_text VARCHAR(64) DEFAULT 'Участвовать'",
    "contest_sponsors_count": "ALTER TABLE auto_combo_posts ADD COLUMN contest_sponsors_count INTEGER DEFAULT 0",
    "contest_sponsor_kind": "ALTER TABLE auto_combo_posts ADD COLUMN contest_sponsor_kind VARCHAR(16) DEFAULT 'service'",
    "contest_own_sponsors": "ALTER TABLE auto_combo_posts ADD COLUMN contest_own_sponsors TEXT DEFAULT '[]'",
    "contest_end_type": "ALTER TABLE auto_combo_posts ADD COLUMN contest_end_type VARCHAR(24) DEFAULT 'participants'",
    "contest_end_value": "ALTER TABLE auto_combo_posts ADD COLUMN contest_end_value INTEGER DEFAULT 0",
    "contest_winners_count": "ALTER TABLE auto_combo_posts ADD COLUMN contest_winners_count INTEGER DEFAULT 1",
}

_COMBO_RUN_COLUMNS = {
    "posted_ids": "ALTER TABLE auto_combo_runs ADD COLUMN posted_ids TEXT DEFAULT '[]'",
    "contest_ids": "ALTER TABLE auto_combo_runs ADD COLUMN contest_ids TEXT DEFAULT '[]'",
}

_USERBOT_COLUMNS = {
    "gift_entities": "ALTER TABLE userbot_accounts ADD COLUMN gift_entities TEXT DEFAULT '[]'",
    "max_greets_hour": "ALTER TABLE userbot_accounts ADD COLUMN max_greets_hour INTEGER DEFAULT 1000",
}


def _migrate_sync(connection) -> None:
    insp = inspect(connection)
    tables = set(insp.get_table_names())
    if "users" in tables:
        cols = {c["name"] for c in insp.get_columns("users")}
        for name, sql in _USER_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    for table in ("ads", "shows", "offers"):
        if table not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        for name, sql in _POST_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql.format(table=table)))
    if "contests" in tables:
        cols = {c["name"] for c in insp.get_columns("contests")}
        for name, sql in _CONTEST_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "channel_greetings" in tables:
        cols = {c["name"] for c in insp.get_columns("channel_greetings")}
        for name, sql in _GREET_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "greeting_posts" in tables:
        cols = {c["name"] for c in insp.get_columns("greeting_posts")}
        for name, sql in _GREET_POST_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "auto_combos" in tables:
        cols = {c["name"] for c in insp.get_columns("auto_combos")}
        for name, sql in _COMBO_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "auto_combo_posts" in tables:
        cols = {c["name"] for c in insp.get_columns("auto_combo_posts")}
        for name, sql in _COMBO_POST_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "auto_combo_runs" in tables:
        cols = {c["name"] for c in insp.get_columns("auto_combo_runs")}
        for name, sql in _COMBO_RUN_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))
    if "userbot_accounts" in tables:
        cols = {c["name"] for c in insp.get_columns("userbot_accounts")}
        for name, sql in _USERBOT_COLUMNS.items():
            if name not in cols:
                connection.execute(text(sql))


async def init_db() -> None:
    global _initialized
    async with _init_lock:
        if _initialized:
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_migrate_sync)
        async with SessionLocal() as session:
            for key, value in DEFAULT_SETTINGS.items():
                existing = await session.get(Setting, key)
                if existing is None:
                    session.add(Setting(key=key, value=value))
            await session.commit()
        from database.models import ChannelGreeting, GreetingPost
        from sqlalchemy import select as sa_select

        async with SessionLocal() as session:
            rows = list((await session.execute(sa_select(ChannelGreeting))).scalars().all())
            for row in rows:
                has_post = (
                    await session.execute(
                        sa_select(GreetingPost.id).where(GreetingPost.greeting_id == row.id).limit(1)
                    )
                ).scalar_one_or_none()
                if has_post:
                    continue
                if (row.copy_chat_id and row.copy_message_id) or (row.text or "").strip() or row.media_file_id:
                    session.add(
                        GreetingPost(
                            greeting_id=row.id,
                            sort_order=0,
                            copy_chat_id=row.copy_chat_id,
                            copy_message_id=row.copy_message_id,
                            extra_buttons=row.extra_buttons or "[]",
                            text=row.text or "",
                            parse_mode=row.parse_mode or "HTML",
                            media_type=row.media_type or "none",
                            media_file_id=row.media_file_id or "",
                            delay_after_seconds=max(0, int(row.post_delay_seconds or 5)),
                        )
                    )
            await session.commit()
        await _migrate_combo_playlist()
        await _migrate_combo_seq()
        await _migrate_greet_post_delays()
        _initialized = True


async def _migrate_combo_playlist() -> None:
    from sqlalchemy import select as sa_select

    from database.crud import get_setting, set_setting
    from database.models import AutoCombo, AutoComboPost

    async with SessionLocal() as session:
        if (await get_setting(session, "combo_playlist_v2", "")) == "1":
            return
        combos = list((await session.execute(sa_select(AutoCombo))).scalars().all())
        for combo in combos:
            posts = list(
                (
                    await session.execute(
                        sa_select(AutoComboPost)
                        .where(AutoComboPost.combo_id == combo.id)
                        .order_by(AutoComboPost.sort_order, AutoComboPost.id)
                    )
                ).scalars().all()
            )
            has_contest_slot = any((p.kind or "bait") == "contest" for p in posts)
            interval = max(0, int(combo.post_interval_seconds or 0))
            offset = 0
            for post in posts:
                if not post.kind:
                    post.kind = "bait"
                if int(post.offset_seconds or 0) == 0 and offset:
                    post.offset_seconds = offset
                offset = int(post.offset_seconds or 0) + interval
            if (combo.contest_text or "").strip() and not has_contest_slot:
                session.add(
                    AutoComboPost(
                        combo_id=combo.id,
                        kind="contest",
                        offset_seconds=offset,
                        sort_order=(posts[-1].sort_order + 1) if posts else 0,
                        title="Конкурс",
                        contest_text=combo.contest_text or "",
                        contest_parse_mode=combo.contest_parse_mode or "HTML",
                        contest_media_type=combo.contest_media_type or "photo",
                        contest_media_file_id=combo.contest_media_file_id or "",
                        contest_button_text=combo.contest_button_text or "Участвовать",
                        contest_sponsors_count=int(combo.contest_sponsors_count or 0),
                        contest_sponsor_kind=combo.contest_sponsor_kind or "service",
                        contest_own_sponsors=combo.contest_own_sponsors or "[]",
                        contest_end_type=combo.contest_end_type or "participants",
                        contest_end_value=int(combo.contest_end_value or 0),
                        contest_winners_count=int(combo.contest_winners_count or 1),
                    )
                )
                offset += max(0, int(combo.after_contest_seconds or 0))
            bait_ready = bool(
                (combo.bait_copy_chat_id and combo.bait_copy_message_id)
                or (combo.bait_text or "").strip()
                or combo.bait_media_file_id
            )
            if bait_ready:
                session.add(
                    AutoComboPost(
                        combo_id=combo.id,
                        kind="bait",
                        offset_seconds=offset,
                        sort_order=offset,
                        title=combo.bait_title or "Байт",
                        copy_chat_id=combo.bait_copy_chat_id,
                        copy_message_id=combo.bait_copy_message_id,
                        extra_buttons=combo.bait_extra_buttons or "[]",
                        text=combo.bait_text or "",
                        parse_mode=combo.bait_parse_mode or "HTML",
                        media_type=combo.bait_media_type or "none",
                        media_file_id=combo.bait_media_file_id or "",
                    )
                )
                offset += max(0, int(combo.bait_lifetime_seconds or 0))
            if int(combo.cycle_seconds or 0) <= 0:
                combo.cycle_seconds = max(36000, offset)
        await set_setting(session, "combo_playlist_v2", "1")
        await session.commit()


async def _migrate_combo_seq() -> None:
    from sqlalchemy import select as sa_select

    from database.crud import get_setting, set_setting, utcnow
    from database.models import AutoCombo, AutoComboPost

    async with SessionLocal() as session:
        if (await get_setting(session, "combo_seq_v1", "")) == "1":
            return
        combos = list((await session.execute(sa_select(AutoCombo))).scalars().all())
        for combo in combos:
            if (combo.phase or "") == "play":
                combo.phase = "idle"
                combo.next_at = None
                combo.current_run_id = None
                combo.warmup_index = 0
                combo.updated_at = utcnow()
            if int(combo.spam_seconds or 0) <= 0:
                combo.spam_seconds = 10800
            if int(combo.post_interval_seconds or 0) <= 0:
                combo.post_interval_seconds = 60
            if int(combo.after_contest_seconds or 0) <= 0:
                combo.after_contest_seconds = 180
            if int(combo.bait_lifetime_seconds or 0) == 600:
                combo.bait_lifetime_seconds = 60
            posts = list(
                (
                    await session.execute(
                        sa_select(AutoComboPost)
                        .where(AutoComboPost.combo_id == combo.id)
                        .order_by(AutoComboPost.offset_seconds, AutoComboPost.sort_order, AutoComboPost.id)
                    )
                ).scalars().all()
            )
            contest_posts = [p for p in posts if (p.kind or "") == "contest"]
            if contest_posts and not (combo.contest_text or "").strip():
                src = contest_posts[0]
                combo.contest_text = src.contest_text or ""
                combo.contest_parse_mode = src.contest_parse_mode or "HTML"
                combo.contest_media_type = src.contest_media_type or "none"
                combo.contest_media_file_id = src.contest_media_file_id or ""
                combo.contest_button_text = src.contest_button_text or "Участвовать"
                combo.contest_sponsors_count = int(src.contest_sponsors_count or 0)
                combo.contest_sponsor_kind = src.contest_sponsor_kind or "service"
                combo.contest_own_sponsors = src.contest_own_sponsors or "[]"
                combo.contest_end_type = src.contest_end_type or "participants"
                combo.contest_end_value = int(src.contest_end_value or 0)
                combo.contest_winners_count = int(src.contest_winners_count or 1)
            contest_offset = None
            if contest_posts:
                contest_offset = min(int(p.offset_seconds or 0) for p in contest_posts)
            for post in posts:
                if (post.kind or "") == "contest":
                    continue
                if contest_offset is not None and int(post.offset_seconds or 0) > contest_offset:
                    post.kind = "after"
                else:
                    post.kind = "pre"
        await set_setting(session, "combo_seq_v1", "1")
        await session.commit()


async def _migrate_greet_post_delays() -> None:
    from sqlalchemy import select as sa_select

    from database.crud import get_setting, set_setting
    from database.models import ChannelGreeting, GreetingPost

    async with SessionLocal() as session:
        if (await get_setting(session, "greet_post_delay_v1", "")) == "1":
            return
        greets = list((await session.execute(sa_select(ChannelGreeting))).scalars().all())
        for greet in greets:
            delay = max(0, int(greet.post_delay_seconds or 5))
            posts = list(
                (
                    await session.execute(
                        sa_select(GreetingPost).where(GreetingPost.greeting_id == greet.id)
                    )
                ).scalars().all()
            )
            for post in posts:
                post.delay_after_seconds = delay
        await set_setting(session, "greet_post_delay_v1", "1")
        await session.commit()


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session
