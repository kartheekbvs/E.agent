
import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode
import sqlite3
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


def get_github_config() -> tuple[str, str, str]:
    """Load and validate GitHub OAuth configuration."""
    client_id = os.getenv("GITHUB_APP_CLIENT_ID")
    client_secret = os.getenv("GITHUB_APP_CLIENT_SECRET")
    callback_url = os.getenv("GITHUB_CALLBACK_URL")

    if not client_id or not client_secret or not callback_url:
        raise RuntimeError(
            "Missing GitHub configuration. Check the three "
            "GITHUB_* variables in your .env file."
        )

    return client_id, client_secret, callback_url


def generate_oauth_state() -> str:
    """Generate an unpredictable value to protect the OAuth flow."""
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    """Return a PKCE verifier and its SHA-256 challenge."""
    verifier = secrets.token_urlsafe(64)

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    return verifier, challenge


def build_authorization_url(state: str, challenge: str) -> str:
    """Build the URL that redirects the user to GitHub."""
    client_id, _, callback_url = get_github_config()

    parameters = {
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
        "scope": "offline_access",
    }

    return f"{GITHUB_AUTHORIZE_URL}?{urlencode(parameters)}"


async def exchange_code_for_token(
    code: str,
    code_verifier: str,
) -> dict:
    """Exchange an OAuth authorization code for a GitHub user token."""
    client_id, client_secret, callback_url = get_github_config()

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": callback_url,
        "code_verifier": code_verifier,
    }

    headers = {
        "Accept": "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            GITHUB_TOKEN_URL,
            data=payload,
            headers=headers,
        )

    response.raise_for_status()
    result = response.json()

    if "error" in result or not result.get("access_token"):
        raise RuntimeError(
            f"GitHub token exchange failed: "
            f"{result.get('error_description', result.get('error', 'Unknown error'))}"
        )

    return result

import time
import hmac

from web.auth import DB_PATH


def initialize_github_oauth_storage() -> None:
    """Create a table for short-lived GitHub OAuth transactions."""
    with __import__("sqlite3").connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS github_oauth_transactions (
                state_hash TEXT PRIMARY KEY,
                verifier TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                browser_nonce_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)


def save_oauth_transaction(
    state: str,
    verifier: str,
    user_id: int,
    browser_nonce: str,
) -> None:
    """Store a short-lived OAuth transaction on the server."""
    initialize_github_oauth_storage()

    state_hash = hashlib.sha256(state.encode()).hexdigest()
    nonce_hash = hashlib.sha256(browser_nonce.encode()).hexdigest()

    with __import__("sqlite3").connect(DB_PATH) as connection:
        connection.execute(
            "DELETE FROM github_oauth_transactions WHERE created_at < ?",
            (int(time.time()) - 600,),
        )
        connection.execute(
            """
            INSERT INTO github_oauth_transactions
                (state_hash, verifier, user_id, browser_nonce_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (state_hash, verifier, user_id, nonce_hash, int(time.time())),
        )


def consume_oauth_transaction(
    state: str,
    browser_nonce: str,
) -> dict | None:
    """Validate and consume a transaction once, within ten minutes."""
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    nonce_hash = hashlib.sha256(browser_nonce.encode()).hexdigest()

    with __import__("sqlite3").connect(DB_PATH) as connection:
        connection.row_factory = __import__("sqlite3").Row
        connection.execute(
            "DELETE FROM github_oauth_transactions WHERE created_at < ?",
            (int(time.time()) - 600,),
        )

        row = connection.execute(
            """
            SELECT verifier, user_id, browser_nonce_hash
            FROM github_oauth_transactions
            WHERE state_hash = ?
            """,
            (state_hash,),
        ).fetchone()

        if row is None:
            return None

        # Consume even an invalid attempt, preventing transaction reuse.
        connection.execute(
            "DELETE FROM github_oauth_transactions WHERE state_hash = ?",
            (state_hash,),
        )

    if not hmac.compare_digest(row["browser_nonce_hash"], nonce_hash):
        return None

    return {
        "verifier": row["verifier"],
        "user_id": row["user_id"],
    }

def _get_token_cipher() -> Fernet:
    key = os.getenv("GITHUB_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is missing from .env"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as error:
        raise RuntimeError("Invalid GitHub token encryption key.") from error


def initialize_github_connections() -> None:
    """Create persistent GitHub connection storage."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS github_connections (
                user_id INTEGER PRIMARY KEY,
                github_user_id INTEGER NOT NULL,
                github_login TEXT NOT NULL,
                access_token_encrypted TEXT NOT NULL,
                refresh_token_encrypted TEXT,
                access_token_expires_at TEXT,
                connected_at TEXT NOT NULL
            )
        """)


def save_github_connection(
    user_id: int,
    github_user: dict,
    token_data: dict,
) -> None:
    """Encrypt and store a user's GitHub connection."""
    cipher = _get_token_cipher()
    initialize_github_connections()

    access_token = token_data.get("access_token")
    if not access_token:
        raise ValueError("GitHub access token is missing.")

    encrypted_access = cipher.encrypt(
        access_token.encode("utf-8")
    ).decode("ascii")

    refresh_token = token_data.get("refresh_token")
    encrypted_refresh = (
        cipher.encrypt(refresh_token.encode("utf-8")).decode("ascii")
        if refresh_token
        else None
    )

    expires_in = token_data.get("expires_in")
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
        if expires_in is not None
        else None
    )

    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("""
            INSERT INTO github_connections (
                user_id, github_user_id, github_login,
                access_token_encrypted, refresh_token_encrypted,
                access_token_expires_at, connected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                github_user_id = excluded.github_user_id,
                github_login = excluded.github_login,
                access_token_encrypted = excluded.access_token_encrypted,
                refresh_token_encrypted = excluded.refresh_token_encrypted,
                access_token_expires_at = excluded.access_token_expires_at,
                connected_at = excluded.connected_at
        """, (
            user_id,
            int(github_user["id"]),
            github_user["login"],
            encrypted_access,
            encrypted_refresh,
            expires_at,
            datetime.now(timezone.utc).isoformat(),
        ))
