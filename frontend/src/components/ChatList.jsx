import React from "react";

export default function ChatList({ chats, activeChatId, onNewChat, onSelectChat }) {
  // Barra lateral con conversaciones persistidas y accion para crear un chat.
  return (
    <aside className="chat-list">
      <div className="chat-list-header">
        <h2>Chats</h2>
        <button type="button" className="primary-button" onClick={onNewChat}>
          Nuevo
        </button>
      </div>
      <div className="chat-items">
        {chats.length === 0 && <p className="empty-state">Sin chats guardados.</p>}
        {chats.map((chat) => (
          <button
            key={chat.id}
            type="button"
            className={`chat-item ${chat.id === activeChatId ? "active" : ""}`}
            onClick={() => onSelectChat(chat.id)}
          >
            <span>{chat.title}</span>
            <small>{new Date(chat.updated_at).toLocaleString()}</small>
          </button>
        ))}
      </div>
    </aside>
  );
}
