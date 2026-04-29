from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)


def create_tables() -> None:
    SQLModel.metadata.create_all(engine)
    # Add columns introduced after initial table creation (safe to run repeatedly)
    _migrate()


def _migrate() -> None:
    """Idempotent column additions for SQLite (no Alembic yet)."""
    additions = [
        ("classification", "sender_type", "TEXT NOT NULL DEFAULT 'unknown'"),
    ]
    with engine.connect() as conn:
        for table, col, definition in additions:
            existing = [
                row[1]
                for row in conn.execute(
                    __import__("sqlalchemy").text(f"PRAGMA table_info({table})")
                )
            ]
            if col not in existing:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                    )
                )
                conn.commit()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
