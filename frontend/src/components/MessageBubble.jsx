import React from "react";

export default function MessageBubble({ message }) {
  // Renderiza mensajes del usuario y del asistente; muestra Cypher si existe.
  const isUser = message.role === "user";

  return (
    <div className={`message-row ${isUser ? "user" : "assistant"}`}>
      <article className={`message-bubble ${isUser ? "user" : "assistant"}`}>
        {!isUser && <div className="message-author">Asistente Neo4j</div>}
        <p>{message.content}</p>
        {!isUser && message.cypher_query && (
          <details className="cypher-details">
            <summary>Cypher ejecutado</summary>
            <pre>{message.cypher_query}</pre>
          </details>
        )}
      </article>
    </div>
  );
}
