from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from ..config import settings


class TokenError(Exception):
    pass


def create_access_token(subject: str) -> tuple[str, int]:
    expires_seconds = settings.jwt_expires_minutes * 60
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
    payload = {"sub": subject, "exp": expires_at}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_seconds


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        raise TokenError("invalid token") from e

    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise TokenError("malformed token")
    return sub
