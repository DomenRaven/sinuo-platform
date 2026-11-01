"""Method card versions for revision 0009. Rows are insert-only."""

from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sinuo.infrastructure.models import Base


class MethodCard(Base):
    __tablename__ = "method_cards"
    __table_args__ = (
        UniqueConstraint("library_key", "card_key", "version_no", name="uq_method_cards_version"),
        CheckConstraint(
            "(library_key = 'course' AND project_id IS NULL) OR "
            "(project_id IS NOT NULL AND library_key = project_id)",
            name="ck_method_cards_library",
        ),
        CheckConstraint("version_no >= 1", name="ck_method_cards_version"),
        CheckConstraint("length(trim(name)) > 0", name="ck_method_cards_name"),
        CheckConstraint("length(trim(card_key)) > 0", name="ck_method_cards_key"),
        CheckConstraint(
            "json_valid(source_json) AND json_type(source_json) = 'object'",
            name="ck_method_cards_source",
        ),
        CheckConstraint(
            "json_valid(applicable_json) AND json_type(applicable_json) = 'object'",
            name="ck_method_cards_applicable",
        ),
        CheckConstraint(
            "json_valid(inputs_json) AND json_type(inputs_json) = 'array'",
            name="ck_method_cards_inputs",
        ),
        CheckConstraint(
            "json_valid(steps_json) AND json_type(steps_json) = 'array'",
            name="ck_method_cards_steps",
        ),
        CheckConstraint(
            "json_valid(outputs_json) AND json_type(outputs_json) = 'array'",
            name="ck_method_cards_outputs",
        ),
        CheckConstraint(
            "json_valid(limits_json) AND json_type(limits_json) = 'array'",
            name="ck_method_cards_limits",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    library_key: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "projects.id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
            name="fk_method_cards_project",
        ),
        nullable=True,
    )
    card_key: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    source_json: Mapped[str] = mapped_column(Text)
    applicable_json: Mapped[str] = mapped_column(Text)
    inputs_json: Mapped[str] = mapped_column(Text)
    steps_json: Mapped[str] = mapped_column(Text)
    outputs_json: Mapped[str] = mapped_column(Text)
    limits_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
