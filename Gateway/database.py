from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Manual .env loading
env_vars = {}
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env_vars[key.strip()] = val.strip()

DATABASE_URL = env_vars.get("DATABASE_URL", os.getenv("DATABASE_URL", "mysql+pymysql://root:123456@localhost:3306/schoolbus"))

engine = None
try:
    if DATABASE_URL.startswith("mysql"):
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        conn = engine.connect()
        conn.close()
        print(f"[Gateway DB] Connected to MySQL database: {DATABASE_URL}")
    else:
        raise ValueError("Fallback to SQLite")
except Exception as e:
    print(f"[Gateway DB WARNING] MySQL connection failed: {e}. Falling back to SQLite local database.")
    DATABASE_URL = "sqlite:///./schoolbus.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
