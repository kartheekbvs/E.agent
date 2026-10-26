import sqlite3
import hashlib
import secrets
import os
from pathlib import Path
import hmac

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR /"data"
DB_PATH = Path(
    os.getenv("ENTROPY_DB_PATH", str(DATA_DIR / "entropy.db"))
)


def initialize_database():
    "Create the database and users table if they do not exist"

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
        create table if not exists users(
        id integer primary key autoincrement,
        email text not null unique,
        password_hash text not null unique,
        salt text not null,
        created_at text not null default current_timestamp
        )
        """)
    return DB_PATH
def register_user(email : str , password : str)->tuple[bool,str]:
    "Register a user with a securly hashed password."
    email = email.strip().lower()
    if not email or "@" not in email:
        return False , "Please enter a valid email address."
    if len(password) < 8:
        return False , "Password must contain at least 8 characters."
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,

    )
    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute(
                """
                insert into users (email,password_hash,salt) values(?,?,?)
                """,
                (
                    email,
                    password_hash.hex(),
                    salt.hex(),
                ),
            )
        return True,"Account created successfully"
    except sqlite3.IntegrityError:
        return False,"An account with this email already exists."

def login_user(email: str, password: str) -> tuple[bool, str]:
    """Verify a user's email and password."""

    email = email.strip().lower()

    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT password_hash, salt
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()

    if row is None:
        return False, "Invalid email or password."

    stored_hash, stored_salt = row

    salt = bytes.fromhex(stored_salt)

    entered_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )

    if not hmac.compare_digest(entered_hash.hex(), stored_hash):
        return False, "Invalid email or password."

    return True, "Login successful."
def get_user_id(email: str) -> int | None:
    email = email.strip().lower()

    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,),
        ).fetchone()

    return row[0] if row else None