from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class MessageRead(BaseModel):
    id: int
    chat_id: int
    role: str
    content: str
    cypher_query: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=180)


class ChatRead(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChatDetail(ChatRead):
    messages: list[MessageRead] = []


class AssistantResponse(BaseModel):
    user_message: MessageRead
    assistant_message: MessageRead
