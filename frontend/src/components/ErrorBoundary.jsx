import React from "react";

export default class ErrorBoundary extends React.Component {
  // Error Boundary de React: captura errores de renderizado en sus hijos.
  constructor(props) {
    super(props);
    this.state = { hasError: false, message: "" };
  }

  static getDerivedStateFromError(error) {
    // Actualiza el estado para mostrar una pantalla de fallback.
    return { hasError: true, message: error?.message || "Error inesperado en la interfaz." };
  }

  componentDidCatch(error, errorInfo) {
    // Registra detalles utiles para depurar sin romper toda la aplicacion.
    console.error("Error capturado por ErrorBoundary:", error, errorInfo);
  }

  render() {
    // Muestra una UI simple si algun componente falla durante el renderizado.
    if (this.state.hasError) {
      return (
        <main className="app-error">
          <h1>No se pudo cargar la interfaz</h1>
          <p>{this.state.message}</p>
          <button type="button" onClick={() => window.location.reload()}>
            Recargar
          </button>
        </main>
      );
    }

    return this.props.children;
  }
}
