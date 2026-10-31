"""Rows for stored_objects and object_pins. The migration owns the constraints."""

from sqlalchemy import Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from sinuo.infrastructure.assets.records import AssetBase


class StoredObjectRow(AssetBase):
    __tablename__ = "stored_objects"

    object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    generation_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    fencing_token: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class ObjectPinRow(AssetBase):
    __tablename__ = "object_pins"

    pin_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    upload_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[float] = mapped_column(Float)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)
