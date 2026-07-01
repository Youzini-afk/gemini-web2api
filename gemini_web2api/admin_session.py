"""Admin session management: password auth + in-memory session tokens."""
import hmac
import secrets
import threading
import time

from .config import CONFIG

_sessions: dict = {}  # token -> expiry timestamp
_sessions_lock = threading.Lock()
_SESSION_TTL = 86400  # 24 hours


def _get_admin_password() -> str:
    return str(CONFIG.get("admin_password") or "")


def verify_password(password: str) -> bool:
    """Constant-time password comparison."""
    stored = _get_admin_password()
    if not stored:
        return False
    return hmac.compare_digest(password, stored)


def create_session() -> str:
    """Create a new session token and return it."""
    token = secrets.token_hex(16)
    with _sessions_lock:
        _cleanup_expired_locked()
        _sessions[token] = time.time() + _SESSION_TTL
    return token


def validate_session(token: str) -> bool:
    """Check if a session token is valid and not expired."""
    if not token:
        return False
    with _sessions_lock:
        expiry = _sessions.get(token)
        if expiry is None:
            return False
        if time.time() > expiry:
            del _sessions[token]
            return False
        return True


def destroy_session(token: str):
    """Destroy a session token."""
    with _sessions_lock:
        _sessions.pop(token, None)


def _cleanup_expired_locked():
    """Remove expired sessions. Caller must hold _sessions_lock."""
    now = time.time()
    expired = [t for t, exp in _sessions.items() if now > exp]
    for t in expired:
        del _sessions[t]


def cleanup_expired():
    """Public cleanup for periodic calling."""
    with _sessions_lock:
        _cleanup_expired_locked()
