import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings


class JsonFormatter(logging.Formatter):
    """
    Formats log records as JSON lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        data = getattr(record, "data", None)

        if isinstance(data, dict):
            payload.update(data)

        return json.dumps(payload, default=str)


def setup_logging(
    level: str = "INFO",
    file_path: str = "",
) -> logging.Logger:
    """
    Configure the Relay logger once, emitting JSON lines to stdout and
    optionally to a log file.
    """

    logger = logging.getLogger("relay")

    if getattr(logger, "_relay_configured", False):
        return logger

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonFormatter())
    logger.addHandler(stream_handler)

    if file_path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    logger._relay_configured = True

    return logger


class RequestLogger:
    """
    Emits structured JSON log records for Relay activity.
    """

    def __init__(
        self,
        level: str = "",
        file_path: str = "",
    ) -> None:
        self._logger = setup_logging(
            level or settings.log_level,
            file_path or settings.log_file,
        )

    def _emit(self, event: str, **data) -> None:
        self._logger.info(event, extra={"data": data})

    def attempt(self, **fields) -> None:
        """
        Log a single provider/model attempt.
        """
        self._emit("attempt", **fields)

    def request(self, **fields) -> None:
        """
        Log the final outcome of a chat request.
        """
        self._emit("request", **fields)

    def chat(self, result: dict) -> None:
        """
        Log a chat request: one record per attempt plus a final record.

        Only metadata is logged (provider/model/latency/outcomes plus the
        ephemeral correlation id); prompt and response content is never
        captured.
        """
        correlation_id = result.get("correlation_id")

        continuity = result.get("continuity")
        conversation_id = (
            continuity.get("conversation_id")
            if isinstance(continuity, dict)
            else None
        )
        switched = (
            continuity.get("switched", False)
            if isinstance(continuity, dict)
            else False
        )

        for attempt in result.get("attempts", []):
            self.attempt(
                provider=attempt.get("provider"),
                model=attempt.get("model"),
                attempt=attempt.get("attempt"),
                latency_ms=attempt.get("latency_ms"),
                success=attempt.get("success"),
                failure_type=attempt.get("failure_type"),
                reason=attempt.get("reason"),
                correlation_id=correlation_id,
                conversation_id=conversation_id,
            )

        self.request(
            provider=result.get("provider"),
            model=result.get("model"),
            latency_ms=result.get("latency_ms"),
            success=result.get("success", False),
            fallback_reason=result.get("fallback_reason"),
            error=result.get("error"),
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            switched=switched,
        )
