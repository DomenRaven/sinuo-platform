"""Suggestion preference and trigger rows for revision 0006."""

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sinuo.infrastructure.models import Base


class SuggestionPreference(Base):
    __tablename__ = "suggestion_preferences"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('off', 'manual', 'event', 'digest')",
            name="ck_suggestion_preferences_mode",
        ),
        CheckConstraint("revision >= 1", name="ck_suggestion_preferences_revision"),
        CheckConstraint("cooldown_seconds >= 0", name="ck_suggestion_preferences_cooldown"),
        CheckConstraint(
            "digest_interval_seconds > 0",
            name="ck_suggestion_preferences_interval",
        ),
        CheckConstraint(
            "json_valid(scope_json) AND json_type(scope_json) = 'object'",
            name="ck_suggestion_preferences_scope",
        ),
    )

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_suggestion_preferences_project",
        ),
        primary_key=True,
    )
    actor_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "accounts.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_suggestion_preferences_actor",
        ),
        primary_key=True,
    )
    mode: Mapped[str] = mapped_column(String(16))
    scope_json: Mapped[str] = mapped_column(Text)
    cooldown_seconds: Mapped[int] = mapped_column(Integer)
    digest_interval_seconds: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    next_due_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float)


class SuggestionTrigger(Base):
    __tablename__ = "suggestion_triggers"
    __table_args__ = (
        ForeignKeyConstraint(
            ("project_id", "actor_id"),
            ("suggestion_preferences.project_id", "suggestion_preferences.actor_id"),
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_suggestion_triggers_preference",
        ),
        CheckConstraint(
            "trigger_kind IN ('event', 'digest')",
            name="ck_suggestion_triggers_kind",
        ),
        CheckConstraint(
            "outcome IN ('queued', 'coalesced', 'suppressed')",
            name="ck_suggestion_triggers_outcome",
        ),
        CheckConstraint(
            "(outcome = 'queued' AND job_id IS NOT NULL) OR "
            "(outcome IN ('coalesced', 'suppressed') AND job_id IS NULL)",
            name="ck_suggestion_triggers_job",
        ),
        CheckConstraint("preference_revision >= 1", name="ck_suggestion_triggers_revision"),
        CheckConstraint("length(trim(trigger_key)) > 0", name="ck_suggestion_triggers_key"),
    )

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    preference_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    trigger_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    outcome: Mapped[str] = mapped_column(String(16))
    job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "jobs.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_suggestion_triggers_job",
        ),
        nullable=True,
    )
    created_at: Mapped[float] = mapped_column(Float)
