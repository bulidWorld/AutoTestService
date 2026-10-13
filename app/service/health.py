from psycopg import Error

from app.repository.database import check_database


def readiness() -> bool:
    try:
        check_database()
        return True
    except (Error, OSError, ValueError):
        return False
