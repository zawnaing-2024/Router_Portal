from app import create_app, _run_sqlite_migrations
from models import db


def main() -> None:
    app = create_app()
    with app.app_context():
        # Create any missing tables, then run lightweight migrations
        db.create_all()
        _run_sqlite_migrations()
    print("Schema migration done")


if __name__ == '__main__':
    main()


