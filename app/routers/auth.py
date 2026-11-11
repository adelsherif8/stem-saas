from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import Token, UserCreate, UserOut
from ..security import create_access_token, hash_password, verify_password
from ..services import create_user_with_workspace

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = create_user_with_workspace(db, payload.email, hash_password(payload.password))
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == form.username).first()
    ok = bool(
        user
        and user.hashed_password
        and verify_password(form.password, user.hashed_password)
    )
    if user is not None:
        # Login events land in the user's personal workspace audit log.
        ws = user.personal_workspace
        audit.record(
            db,
            "auth.login" if ok else "auth.login_failed",
            workspace_id=ws.id if ws else None,
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
            details={"method": "password"},
            request=request,
        )
        db.commit()
    if not ok:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return Token(access_token=create_access_token(user.email))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
