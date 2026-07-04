"""
auth.py — Authentification par email/mot de passe + token de session.

- Hash de mot de passe : PBKDF2-HMAC-SHA256 (bibliotheque standard hashlib,
  aucune dependance externe type bcrypt — evite tout risque d echec de build
  sur Railway lie a une extension C).
- Token de session : JWT (bibliotheque PyJWT, pure Python, tres standard).

Variable d environnement requise :
  HYPERBOT_SECRET_KEY — cle secrete pour signer les tokens. A definir sur
  Railway (une longue chaine aleatoire). Sans elle, une cle par defaut
  (non securisee) est utilisee — uniquement pour les tests locaux.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT

SECRET_KEY = os.environ.get("HYPERBOT_SECRET_KEY", "insecure-dev-key-change-me")
TOKEN_TTL_HOURS = int(os.environ.get("HYPERBOT_TOKEN_TTL_HOURS", "168"))  # 7 jours par defaut

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, hex_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hex_digest)


def create_token(email: str) -> str:
    payload = {
        "sub": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str):
    """Retourne l email si le token est valide, sinon None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None
