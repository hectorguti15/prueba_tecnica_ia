import React, { useEffect, useState } from "react";

import { createChat, getChat, listChats, sendMessage } from "./api";
import ChatList from "./components/ChatList";
import ChatWindow from "./components/ChatWindow";

export default function App() {
  // Estado principal de la pantalla: chats, conversacion activa y mensajes visibles.
  const [chats, setChats] = useState([]);
  const [activeChat, setActiveChat] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    refreshChats();
  }, []);

  async function refreshChats() {
    // Sincroniza la barra lateral con los chats persistidos en PostgreSQL.
    try {
      const data = await listChats();
      setChats(data);
    } catch (apiError) {
      setError(apiError.message || "No se pudieron cargar los chats.");
    }
  }

  async function handleNewChat() {
    // Crea una conversacion vacia para empezar una consulta nueva.
    setError("");
    try {
      const chat = await createChat("Nuevo chat");
      setActiveChat(chat);
      setMessages([]);
      await refreshChats();
    } catch (apiError) {
      setError(apiError.message || "No se pudo crear el chat.");
    }
  }

  async function handleSelectChat(chatId) {
    // Carga un chat anterior y sus mensajes para continuar con memoria de sesion.
    setError("");
    try {
      const chat = await getChat(chatId);
      setActiveChat(chat);
      setMessages(chat.messages || []);
    } catch (apiError) {
      setError(apiError.message || "No se pudo cargar el chat.");
    }
  }

  async function handleSendMessage(content) {
    // Envia el mensaje al backend; el backend guarda historial, consulta Neo4j y responde.
    setError("");
    setLoading(true);

    try {
      let chat = activeChat;
      if (!chat) {
        chat = await createChat(content.slice(0, 80));
        setActiveChat(chat);
      }

      const optimisticUserMessage = {
        id: `temp-${Date.now()}`,
        chat_id: chat.id,
        role: "user",
        content,
        created_at: new Date().toISOString(),
      };
      setMessages((current) => [...current, optimisticUserMessage]);

      const response = await sendMessage(chat.id, content);
      setMessages((current) => [
        ...current.filter((message) => message.id !== optimisticUserMessage.id),
        response.user_message,
        response.assistant_message,
      ]);

      const updatedChat = await getChat(chat.id);
      setActiveChat(updatedChat);
      await refreshChats();
    } catch (apiError) {
      setError(apiError.message || "No se pudo enviar el mensaje.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="app-shell">
      <ChatList
        chats={chats}
        activeChatId={activeChat?.id}
        onNewChat={handleNewChat}
        onSelectChat={handleSelectChat}
      />
      <section className="chat-area">
        <header className="topbar">
          <div>
            <p className="eyebrow">Agente IA</p>
            <h1>{activeChat?.title || "Consulta publicaciones academicas"}</h1>
          </div>
        </header>
        {error && <div className="error-banner">{error}</div>}
        <ChatWindow messages={messages} loading={loading} onSendMessage={handleSendMessage} />
      </section>
    </main>
  );
}
