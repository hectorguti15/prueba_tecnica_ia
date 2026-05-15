from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Chat
from app.schemas import ChatCreate, ChatDetail, ChatRead

router = APIRouter(prefix="/chats", tags=["chats"])


@router.post("", response_model=ChatRead)
def create_chat(payload: ChatCreate | None = None, db: Session = Depends(get_db)):
    """Crea una conversacion nueva en PostgreSQL."""

    title = payload.title if payload and payload.title else "Nuevo chat"
    chat = Chat(title=title)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


@router.get("", response_model=list[ChatRead])
def list_chats(db: Session = Depends(get_db)):
    """Lista chats persistidos ordenados por ultima actividad."""

    return db.query(Chat).order_by(desc(Chat.updated_at)).all()


@router.get("/{chat_id}", response_model=ChatDetail)
def get_chat(chat_id: int, db: Session = Depends(get_db)):
    """Carga un chat con sus mensajes para reinyectar contexto al agente."""

    chat = (
        db.query(Chat)
        .options(selectinload(Chat.messages))
        .filter(Chat.id == chat_id)
        .first()
    )
    if not chat:
        raise HTTPException(status_code=404, detail="Chat no encontrado")
    return chat
