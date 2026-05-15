import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "backend.log"


def sanitize_log_text(value: object) -> str:
    """Redacta API keys u otros tokens sensibles antes de escribir logs."""

    text = str(value)
    text = re.sub(r"api_key:[^'\"}\s]+", "api_key:[REDACTED]", text)
    text = re.sub(r"AIza[0-9A-Za-z_\-]+", "[REDACTED_API_KEY]", text)
    text = re.sub(r"gsk_[0-9A-Za-z_\-]+", "[REDACTED_GROQ_KEY]", text)
    return text


def summarize_exception(exc: Exception) -> str:
    """Devuelve un resumen de error util para depurar sin filtrar secretos."""

    sanitized = sanitize_log_text(exc)
    return sanitized[:700]


def setup_logging() -> None:
    """Configura logs en consola y archivo para depurar el flujo completo."""

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not root_logger.handlers:
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
