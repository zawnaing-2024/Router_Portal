import os
import sys

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
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

except ImportError as e:
    print(f"Import error: {e}")
    print("Please ensure all required packages are installed:")
    print("pip install -r requirements.txt")
    sys.exit(1)


