from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    total_earned: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    total_withdrawn: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    clicks_total: Mapped[int] = mapped_column(Integer, default=0)
    clicks_today: Mapped[int] = mapped_column(Integer, default=0)
    clicks_today_date: Mapped[str] = mapped_column(String(16), default="")
    last_click_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    ref_milestone_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    start_count: Mapped[int] = mapped_column(Integer, default=0)
    clicks_since_task: Mapped[int] = mapped_column(Integer, default=0)
    pending_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_service: Mapped[str] = mapped_column(String(32), default="")
    pending_service_payload: Mapped[str] = mapped_column(Text, default="{}")
    task_cycle_index: Mapped[int] = mapped_column(Integer, default=0)
    pending_contest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_contest_payload: Mapped[str] = mapped_column(Text, default="{}")
    hour_remind_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_greeting_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    referrer: Mapped[User | None] = relationship("User", remote_side="User.id", uselist=False)
    campaign: Mapped[Campaign | None] = relationship("Campaign")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    comment: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CampaignHit(Base):
    __tablename__ = "campaign_hits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    campaign: Mapped[Campaign] = relationship("Campaign")
    user: Mapped[User] = relationship("User")


class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), default="Реклама")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    media_path: Mapped[str] = mapped_column(String(512), default="")
    button_type: Mapped[str] = mapped_column(String(16), default="none")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(512), default="")
    button_color: Mapped[str] = mapped_column(String(16), default="#2AABEE")
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    media_path: Mapped[str] = mapped_column(String(512), default="")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(512), default="")
    check_type: Mapped[str] = mapped_column(String(24), default="view")
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    service_name: Mapped[str] = mapped_column(String(64), default="")
    service_payload: Mapped[str] = mapped_column(Text, default="{}")
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), default="Показ")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    media_path: Mapped[str] = mapped_column(String(512), default="")
    button_type: Mapped[str] = mapped_column(String(16), default="none")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(512), default="")
    button_color: Mapped[str] = mapped_column(String(16), default="#2AABEE")
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShowSend(Base):
    __tablename__ = "show_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OfferCompletion(Base):
    __tablename__ = "offer_completions"
    __table_args__ = (UniqueConstraint("user_id", "offer_id", name="uq_offer_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OfferView(Base):
    __tablename__ = "offer_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Click(Base):
    __tablename__ = "clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reward: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(32), default="wait_friends", index=True)
    invoice_charge_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admin_comment: Mapped[str] = mapped_column(Text, default="")

    user: Mapped[User] = relationship("User")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(String(128), default="")
    telegram_charge_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    kind: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Contest(Base):
    __tablename__ = "contests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="photo")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    button_text: Mapped[str] = mapped_column(String(64), default="Участвовать")
    sponsors_count: Mapped[int] = mapped_column(Integer, default=1)
    channel_id: Mapped[str] = mapped_column(String(64), default="")
    channel_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_type: Mapped[str] = mapped_column(String(24), default="participants")
    end_value: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    winners_count: Mapped[int] = mapped_column(Integer, default=1)
    winner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    winner_ids: Mapped[str] = mapped_column(Text, default="[]")
    sponsor_kind: Mapped[str] = mapped_column(String(16), default="service")
    own_sponsors: Mapped[str] = mapped_column(Text, default="[]")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    winner: Mapped[User | None] = relationship("User", foreign_keys=[winner_user_id])


class SavedChannel(Base):
    __tablename__ = "saved_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    chat_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelNamer(Base):
    __tablename__ = "channel_namers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    names: Mapped[str] = mapped_column(Text, default="[]")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=10)
    original_title: Mapped[str] = mapped_column(String(255), default="")
    name_index: Mapped[int] = mapped_column(Integer, default=0)
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelGreeting(Base):
    __tablename__ = "channel_greetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    greet_text: Mapped[str] = mapped_column(Text, default="Приветикк! хочешь мишку?")
    post_delay_seconds: Mapped[int] = mapped_column(Integer, default=5)
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GreetingPost(Base):
    __tablename__ = "greeting_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    greeting_id: Mapped[int] = mapped_column(ForeignKey("channel_greetings.id", ondelete="CASCADE"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    delay_after_seconds: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    greeting: Mapped[ChannelGreeting] = relationship("ChannelGreeting")


class GreetingLead(Base):
    __tablename__ = "greeting_leads"
    __table_args__ = (UniqueConstraint("greeting_id", "user_id", name="uq_greeting_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    greeting_id: Mapped[int] = mapped_column(ForeignKey("channel_greetings.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    greeted: Mapped[bool] = mapped_column(Boolean, default=False)
    replied: Mapped[bool] = mapped_column(Boolean, default=False)
    posted: Mapped[bool] = mapped_column(Boolean, default=False)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    greeted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    greeting: Mapped[ChannelGreeting] = relationship("ChannelGreeting")
    user: Mapped[User] = relationship("User")


class SavedResource(Base):
    __tablename__ = "saved_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SpamJob(Base):
    __tablename__ = "spam_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), default="post", index=True)
    title: Mapped[str] = mapped_column(String(128), default="Спам")
    channel_id: Mapped[str] = mapped_column(String(64), default="")
    lifetime_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    pause_seconds: Mapped[int] = mapped_column(Integer, default=300)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    contest_id: Mapped[int | None] = mapped_column(ForeignKey("contests.id", ondelete="SET NULL"), nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phase: Mapped[str] = mapped_column(String(16), default="idle")
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contest: Mapped[Contest | None] = relationship("Contest")


class AutoCombo(Base):
    __tablename__ = "auto_combos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), default="Авто-конкурс")
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    spam_seconds: Mapped[int] = mapped_column(Integer, default=10800)
    cycle_seconds: Mapped[int] = mapped_column(Integer, default=36000)
    post_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    after_contest_seconds: Mapped[int] = mapped_column(Integer, default=180)
    bait_lifetime_seconds: Mapped[int] = mapped_column(Integer, default=60)
    contest_text: Mapped[str] = mapped_column(Text, default="")
    contest_parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    contest_media_type: Mapped[str] = mapped_column(String(16), default="photo")
    contest_media_file_id: Mapped[str] = mapped_column(String(256), default="")
    contest_button_text: Mapped[str] = mapped_column(String(64), default="Участвовать")
    contest_sponsors_count: Mapped[int] = mapped_column(Integer, default=0)
    contest_sponsor_kind: Mapped[str] = mapped_column(String(16), default="service")
    contest_own_sponsors: Mapped[str] = mapped_column(Text, default="[]")
    contest_end_type: Mapped[str] = mapped_column(String(24), default="participants")
    contest_end_value: Mapped[int] = mapped_column(Integer, default=0)
    contest_winners_count: Mapped[int] = mapped_column(Integer, default=1)
    bait_title: Mapped[str] = mapped_column(String(128), default="Байт")
    bait_copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bait_copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bait_extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    bait_text: Mapped[str] = mapped_column(Text, default="")
    bait_parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    bait_media_type: Mapped[str] = mapped_column(String(16), default="none")
    bait_media_file_id: Mapped[str] = mapped_column(String(256), default="")
    phase: Mapped[str] = mapped_column(String(16), default="idle")
    next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warmup_index: Mapped[int] = mapped_column(Integer, default=0)
    current_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_contest_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AutoComboPost(Base):
    __tablename__ = "auto_combo_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    combo_id: Mapped[int] = mapped_column(ForeignKey("auto_combos.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="bait")
    offset_seconds: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(128), default="Пост")
    contest_text: Mapped[str] = mapped_column(Text, default="")
    contest_parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    contest_media_type: Mapped[str] = mapped_column(String(16), default="photo")
    contest_media_file_id: Mapped[str] = mapped_column(String(256), default="")
    contest_button_text: Mapped[str] = mapped_column(String(64), default="Участвовать")
    contest_sponsors_count: Mapped[int] = mapped_column(Integer, default=0)
    contest_sponsor_kind: Mapped[str] = mapped_column(String(16), default="service")
    contest_own_sponsors: Mapped[str] = mapped_column(Text, default="[]")
    contest_end_type: Mapped[str] = mapped_column(String(24), default="participants")
    contest_end_value: Mapped[int] = mapped_column(Integer, default=0)
    contest_winners_count: Mapped[int] = mapped_column(Integer, default=1)
    copy_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    copy_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_buttons: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    combo: Mapped[AutoCombo] = relationship("AutoCombo")


class AutoComboRun(Base):
    __tablename__ = "auto_combo_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    combo_id: Mapped[int] = mapped_column(ForeignKey("auto_combos.id", ondelete="CASCADE"), index=True)
    contest_id: Mapped[int | None] = mapped_column(ForeignKey("contests.id", ondelete="SET NULL"), nullable=True)
    channel_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    posted_mids: Mapped[str] = mapped_column(Text, default="[]")
    posted_ids: Mapped[str] = mapped_column(Text, default="[]")
    contest_ids: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    contest_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bait_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    combo: Mapped[AutoCombo] = relationship("AutoCombo")
    contest: Mapped[Contest | None] = relationship("Contest")


class AutoComboHit(Base):
    __tablename__ = "auto_combo_hits"
    __table_args__ = (UniqueConstraint("run_id", "user_id", name="uq_combo_hit_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("auto_combo_runs.id", ondelete="CASCADE"), index=True)
    combo_id: Mapped[int] = mapped_column(ForeignKey("auto_combos.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    run: Mapped[AutoComboRun] = relationship("AutoComboRun")
    combo: Mapped[AutoCombo] = relationship("AutoCombo")
    user: Mapped[User] = relationship("User")


class ContestParticipant(Base):
    __tablename__ = "contest_participants"
    __table_args__ = (UniqueConstraint("contest_id", "user_id", name="uq_contest_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contest: Mapped[Contest] = relationship("Contest")
    user: Mapped[User] = relationship("User")


class ContestHit(Base):
    __tablename__ = "contest_hits"
    __table_args__ = (UniqueConstraint("contest_id", "user_id", name="uq_contest_hit_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    contest: Mapped[Contest] = relationship("Contest")
    user: Mapped[User] = relationship("User")


class ContestSub(Base):
    __tablename__ = "contest_subs"
    __table_args__ = (UniqueConstraint("contest_id", "user_id", "url", name="uq_contest_sub"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(32), default="", index=True)
    url: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    contest: Mapped[Contest] = relationship("Contest")
    user: Mapped[User] = relationship("User")


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(Text, default="")
    parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    media_type: Mapped[str] = mapped_column(String(16), default="none")
    media_file_id: Mapped[str] = mapped_column(String(256), default="")
    media_path: Mapped[str] = mapped_column(String(512), default="")
    button_text: Mapped[str] = mapped_column(String(64), default="")
    button_url: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserBotAccount(Base):
    __tablename__ = "userbot_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    session_string: Mapped[str] = mapped_column(Text, default="")
    tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    first_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="new")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    hello_text: Mapped[str] = mapped_column(Text, default="привеееет, ты хочешь подарочек?")
    gift_text: Mapped[str] = mapped_column(Text, default="")
    gift_parse_mode: Mapped[str] = mapped_column(String(16), default="HTML")
    gift_entities: Mapped[str] = mapped_column(Text, default="[]")
    max_greets_hour: Mapped[int] = mapped_column(Integer, default=1000)
    nudge_30_text: Mapped[str] = mapped_column(Text, default="ну что там???")
    nudge_6h_text: Mapped[str] = mapped_column(Text, default="эййййй, скоро кончатся призы!!!")
    nudge_24h_text: Mapped[str] = mapped_column(Text, default="ну что там???")
    last_error: Mapped[str] = mapped_column(Text, default="")
    greets_hour: Mapped[int] = mapped_column(Integer, default=0)
    greets_hour_key: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserBotLead(Base):
    __tablename__ = "userbot_leads"
    __table_args__ = (UniqueConstraint("account_id", "peer_tg_id", name="uq_userbot_peer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("userbot_accounts.id", ondelete="CASCADE"), index=True)
    peer_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    first_name: Mapped[str] = mapped_column(String(128), default="")
    stage: Mapped[str] = mapped_column(String(24), default="greeted", index=True)
    greeted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gift_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gift_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_user_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_bot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hello_replied: Mapped[bool] = mapped_column(Boolean, default=False)
    gift_replied: Mapped[bool] = mapped_column(Boolean, default=False)
    nudge_30_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    nudge_6h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    nudge_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    account: Mapped[UserBotAccount] = relationship("UserBotAccount")
