from fastapi import APIRouter, HTTPException, status

from ..config import settings
from ..core.security import create_access_token
from ..schemas import LoginRequest, TokenResponse, UserOut
from .deps import CurrentUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    if (
        payload.email.lower() != settings.dev_user_email.lower()
        or payload.password != settings.dev_user_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    token, expires = create_access_token(subject=payload.email.lower())
    return TokenResponse(access_token=token, expires_in=expires)


@router.get("/me", response_model=UserOut)
async def me(user_email: CurrentUser) -> UserOut:
    return UserOut(email=user_email)
