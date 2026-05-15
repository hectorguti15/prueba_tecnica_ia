import React from "react";

import MessageBubble from "./MessageBubble";
import MessageInput from "./MessageInput";

export default function ChatWindow({ messages, loading, onSendMessage }) {
  // Contenedor principal del historial visible y la entrada de texto.
  return (
    <div className="chat-window">
      <div className="messages-panel">
        <div className="messages-stack">
          {messages.length === 0 && (
            <div className="welcome-panel">
              <p className="eyebrow">Grafo academico</p>
              <h2>Haz una pregunta sobre publicaciones</h2>
              <p>Consulta autores, areas, paises, palabras clave, venues, anios y citas.</p>
              <div className="prompt-suggestions">
                <span>Publicaciones de Sophie Laurent</span>
                <span>Top 5 mas citadas</span>
                <span>Publicaciones de NLP en 2024</span>
              </div>
            </div>
          )}

          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}

          {loading && (
            <div className="loading-message">
              <span className="loading-dot" />
              Consultando Neo4j
            </div>
          )}
        </div>
      </div>
      <MessageInput disabled={loading} onSendMessage={onSendMessage} />
    </div>
  );
}
