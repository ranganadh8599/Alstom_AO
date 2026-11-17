"""``python -m alstom_ao`` entry point."""

from __future__ import annotations

import uvicorn

from alstom_ao.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "alstom_ao.main:app",
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
