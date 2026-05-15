from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Clase base para declarar modelos SQLAlchemy."""

    pass


def get_db() -> Generator[Session, None, None]:
    """Entrega una sesion PostgreSQL por request y la cierra al finalizar."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Crea las tablas obligatorias si aun no existen en PostgreSQL."""

    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
