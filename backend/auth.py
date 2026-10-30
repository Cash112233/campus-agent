"""本地账号与会话认证。

密码只保存 PBKDF2 哈希；登录后发放短期可撤销的随机会话令牌，令牌数据库中也只保存哈希值。
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from db import get_conn, now

ITERATIONS = 310_000
SESSION_DAYS = 7
USERNAME_RE = re.compile(r"^[\w.-]{2,32}$", re.UNICODE)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_username(username):
    value = str(username or "").strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("用户名需为 2-32 位字母、数字、下划线、点号或短横线")
    return value


def validate_password(password):
    value = str(password or "")
    if len(value) < 8 or len(value) > 128:
        raise ValueError("密码长度需为 8-128 位")
    return value


def _hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password, encoded):
    try:
        scheme, iterations, salt_text, digest_text = str(encoded).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, UnicodeError):
        return False


def _public_user(row):
    return {"id": int(row["id"]), "username": row["username"], "role": row["role"], "school_id": row["school_id"] or "hlby"}


def _ensure_tables():
    """兼容已运行中的旧进程或测试替换的连接，认证表可安全幂等创建。"""
    conn = get_conn()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('users','sessions')").fetchall()}
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            school_id TEXT NOT NULL DEFAULT 'hlby',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "school_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN school_id TEXT NOT NULL DEFAULT 'hlby'")
    conn.commit()


def user_count():
    _ensure_tables()
    return int(get_conn().execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])


def register_first_user(username, password):
    username = _normalize_username(username)
    password = validate_password(password)
    conn = get_conn()
    if user_count():
        raise ValueError("管理员账号已创建，请直接登录")
    try:
        try:
            from config import load_config
            school_id = str(load_config().get("current_school_id") or "hlby")
        except Exception:
            school_id = "hlby"
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,role,school_id,created_at) VALUES(?,?,?,?,?)",
            (username, _hash_password(password), "admin", school_id, now()),
        )
        # 首个账号接管历史单用户对话，避免升级登录系统后旧记录消失。
        conn.execute("UPDATE conversations SET user_id=? WHERE user_id IS NULL", (cur.lastrowid,))
        conn.execute("UPDATE memories SET user_id=? WHERE user_id IS NULL", (cur.lastrowid,))
        conn.commit()
    except Exception as error:
        conn.rollback()
        if "UNIQUE" in str(error).upper():
            raise ValueError("用户名已存在") from error
        raise
    return {"id": cur.lastrowid, "username": username, "role": "admin", "school_id": school_id}


def authenticate(username, password):
    username = _normalize_username(username)
    password = validate_password(password)
    _ensure_tables()
    row = get_conn().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        raise ValueError("用户名或密码错误")
    return _public_user(row)


def create_session(user_id):
    _ensure_tables()
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires = _utc_now() + timedelta(days=SESSION_DAYS)
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions(user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
        (user_id, token_hash, _iso(expires), now()),
    )
    conn.commit()
    return raw, expires


def set_user_school(user_id, school_id):
    _ensure_tables()
    conn = get_conn()
    conn.execute("UPDATE users SET school_id=? WHERE id=?", (str(school_id), user_id))
    conn.commit()


def get_user_by_token(token):
    _ensure_tables()
    value = str(token or "").strip()
    if not value or len(value) > 256:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    row = get_conn().execute(
        "SELECT u.id,u.username,u.role,u.school_id,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
        (digest,),
    ).fetchone()
    if not row:
        return None
    try:
        expired = datetime.fromisoformat(row["expires_at"]) <= _utc_now()
    except (TypeError, ValueError):
        expired = True
    if expired:
        get_conn().execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        get_conn().commit()
        return None
    return {"id": int(row["id"]), "username": row["username"], "role": row["role"], "school_id": row["school_id"] or "hlby"}


def revoke_token(token):
    _ensure_tables()
    value = str(token or "").strip()
    if not value:
        return
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
    conn.commit()


def clear_expired_sessions():
    _ensure_tables()
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_utc_now()),))
    conn.commit()
