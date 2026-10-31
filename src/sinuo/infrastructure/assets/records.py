"""Upload ticket rows. Kept off the foundation Base so its table count stays put."""

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AssetBase(DeclarativeBase):
    pass


class UploadTicketRow(AssetBase):
    __tablename__ = "upload_tickets"

    upload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(String(64))
    expected_size: Mapped[int] = mapped_column(Integer)
    declared_hash: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(255))
    license: Mapped[str] = mapped_column(String(200))
    sensitivity: Mapped[str] = mapped_column(String(32))
    ai_processing_allowed: Mapped[int] = mapped_column(Integer)
    created_by_account_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    closed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
