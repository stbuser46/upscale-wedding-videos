from __future__ import annotations

from webapp.config import load_settings
from webapp.db import migrate


def main() -> None:
    settings = load_settings()
    migrate(settings.database_path)
    print(f"Database ready: {settings.database_path}")


if __name__ == "__main__":
    main()
