import React from "react";

import MessageBubble from "./MessageBubble";
import MessageInput from "./MessageInput";

export default function ChatWindow({ messages, loading, onSendMessage }) {
  // Contenedor principal del historial visible y la entrada de texto.
  return (
    <div className="chat-window">
      <div className="messages-panel">
        {messages.length === 0 && (
          <div className="welcome-panel">
            <h2>Haz una pregunta sobre el grafo</h2>
            <p>Por ejemplo: publicaciones sobre NLP, autores por pais, citas por area o venues por anio.</p>
          </div>
        )}

        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {loading && <div className="loading-message">Consultando Neo4j...</div>}
      </div>
      <MessageInput disabled={loading} onSendMessage={onSendMessage} />
    </div>
  );
}
