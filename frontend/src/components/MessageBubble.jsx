import React from "react";

export default function MessageBubble({ message }) {
  // Renderiza mensajes del usuario y del asistente; muestra Cypher si existe.
  const isUser = message.role === "user";

  return (
    <article className={`message-bubble ${isUser ? "user" : "assistant"}`}>
      <p>{message.content}</p>
      {!isUser && message.cypher_query && (
        <details>
          <summary>Cypher</summary>
          <pre>{message.cypher_query}</pre>
        </details>
      )}
    </article>
  );
}
