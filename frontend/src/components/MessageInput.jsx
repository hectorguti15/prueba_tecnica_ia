import React, { useState } from "react";

export default function MessageInput({ disabled, onSendMessage }) {
  // Controla el texto local antes de enviarlo al backend.
  const [content, setContent] = useState("");

  function handleSubmit(event) {
    // Envia con Enter o boton, evitando mensajes vacios.
    event.preventDefault();
    const trimmed = content.trim();
    if (!trimmed || disabled) {
      return;
    }
    onSendMessage(trimmed);
    setContent("");
  }

  return (
    <form className="message-input" onSubmit={handleSubmit}>
      <div className="composer">
        <textarea
          value={content}
          disabled={disabled}
          rows={2}
          placeholder="Pregunta sobre publicaciones, autores, areas, paises, venues o citas..."
          onChange={(event) => setContent(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              handleSubmit(event);
            }
          }}
        />
        <button type="submit" disabled={disabled || !content.trim()} aria-label="Enviar mensaje">
          Enviar
        </button>
      </div>
    </form>
  );
}
