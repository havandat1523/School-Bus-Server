from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Manual .env loading in case python-dotenv is not installed
env_vars = {}
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env_vars[key.strip()] = val.strip()

# Try to use MySQL with root:123456 as configured
DATABASE_URL = env_vars.get("DATABASE_URL", os.getenv("DATABASE_URL", "mysql+pymysql://root:123456@localhost:3306/schoolbus"))

engine = None
try:
    if DATABASE_URL.startswith("mysql"):
        # Attempt to create a connection pool for MySQL
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        # Test connection to check if MySQL is running
        conn = engine.connect()
        conn.close()
        print(f"[Database] Successfully connected to MySQL database: {DATABASE_URL}")
    else:
        raise ValueError("Fallback to SQLite")
except Exception as e:
    # If MySQL is not running, fallback to SQLite to prevent startup crashes
    print(f"[Database WARNING] MySQL connection failed: {e}. Falling back to SQLite local database.")
    DATABASE_URL = "sqlite:///./schoolbus.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def auto_migrate_db(engine_obj):
    if engine_obj is None:
        return
    try:
        from sqlalchemy import text
        with engine_obj.connect() as conn:
            # Auto add missing columns to attendants table if they don't exist
            columns_to_add = [
                ("citizen_id", "VARCHAR(50) NULL"),
                ("phone", "VARCHAR(20) NULL"),
                ("email", "VARCHAR(100) NULL"),
                ("status", "VARCHAR(20) DEFAULT 'active'"),
                ("duty_status", "VARCHAR(20) DEFAULT 'offduty'")
            ]
            for col_name, col_type in columns_to_add:
                try:
                    conn.execute(text(f"ALTER TABLE attendants ADD COLUMN {col_name} {col_type};"))
                    conn.commit()
                except Exception:
                    pass
            try:
                conn.execute(text("ALTER TABLE students ADD COLUMN status VARCHAR(20) DEFAULT 'active';"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE vehicles ADD COLUMN session_pending_confirm INT DEFAULT 0;"))
                conn.commit()
            except Exception:
                pass
            try:
                conn.execute(text("ALTER TABLE vehicles ADD COLUMN sos_active INT DEFAULT 0;"))
                conn.commit()
            except Exception:
                pass
    except Exception as e:
        print(f"[Database Migration Warning] Auto-migration skipped: {e}")

auto_migrate_db(engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
