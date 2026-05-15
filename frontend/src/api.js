import axios from "axios";

const API_BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

// Endpoints centralizados para evitar strings duplicados en los componentes.
export const ENDPOINTS = {
  chats: "/chats",
  chatById: (chatId) => `/chats/${chatId}`,
  messages: (chatId) => `/chats/${chatId}/messages`,
};

// Cliente Axios compartido con headers comunes para todos los requests.
const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    Accept: "application/json",
    "Content-Type": "application/json",
  },
  timeout: 60000,
});

export class ApiError extends Error {
  // Error normalizado para que la UI pueda mostrar mensajes consistentes.
  constructor(message, status, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function normalizeApiError(error) {
  // Convierte errores Axios o errores inesperados en ApiError.
  if (axios.isAxiosError(error)) {
    const status = error.response?.status || 0;
    const details = error.response?.data;
    const message =
      details?.detail ||
      error.message ||
      "No se pudo completar la solicitud al backend.";

    return new ApiError(message, status, details);
  }

  return new ApiError(error?.message || "Error inesperado en el cliente API.", 0, error);
}

async function request(config) {
  // Ejecuta un request HTTP y devuelve solamente data; los errores salen normalizados.
  try {
    const response = await apiClient.request(config);
    return response.data;
  } catch (error) {
    throw normalizeApiError(error);
  }
}

function normalizeMessage(message) {
  // Asegura que cada mensaje tenga las propiedades que esperan los componentes.
  return {
    id: message.id,
    chat_id: message.chat_id,
    role: message.role,
    content: message.content || "",
    cypher_query: message.cypher_query || null,
    created_at: message.created_at,
  };
}

function normalizeChat(chat) {
  // Asegura una forma estable para chats individuales o listados.
  return {
    id: chat.id,
    title: chat.title || "Nuevo chat",
    created_at: chat.created_at,
    updated_at: chat.updated_at,
    messages: Array.isArray(chat.messages) ? chat.messages.map(normalizeMessage) : [],
  };
}

function normalizeAssistantResponse(response) {
  // Normaliza la respuesta del endpoint de mensajes.
  return {
    user_message: normalizeMessage(response.user_message),
    assistant_message: normalizeMessage(response.assistant_message),
  };
}

export async function createChat(title = "Nuevo chat") {
  // Crea un chat en el backend y devuelve un objeto chat normalizado.
  const data = await request({
    method: "POST",
    url: ENDPOINTS.chats,
    data: { title },
  });
  return normalizeChat(data);
}

export async function listChats() {
  // Recupera la lista de conversaciones guardadas en PostgreSQL.
  const data = await request({
    method: "GET",
    url: ENDPOINTS.chats,
  });
  return Array.isArray(data) ? data.map(normalizeChat) : [];
}

export async function getChat(chatId) {
  // Carga un chat con sus mensajes para continuar la conversacion.
  const data = await request({
    method: "GET",
    url: ENDPOINTS.chatById(chatId),
  });
  return normalizeChat(data);
}

export async function sendMessage(chatId, content) {
  // Envia el mensaje del usuario y recibe la respuesta generada por el agente.
  const data = await request({
    method: "POST",
    url: ENDPOINTS.messages(chatId),
    data: { content },
  });
  return normalizeAssistantResponse(data);
}
