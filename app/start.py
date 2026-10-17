from __future__ import annotations

import uvicorn

from app.config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=settings.port, workers=1, reload=False)


if __name__ == "__main__":
    main()

