from datetime import datetime, timedelta
import os
import re
import shutil
from fastapi import FastAPI, Depends, HTTPException, File, Form, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from sqlmodel import Field, SQLModel, Session, create_engine, select
from contextlib import asynccontextmanager

DATABASE_URL = "sqlite:///kivansag.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Mappák a fájlokhoz
UPLOAD_DIR = "uploads"
ARCHIVE_DIR = "archived_evidence"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    username: str
    phone: str
    wish: str  # Szöveges kívánság vagy "[Videós Kívánság]" jelölés
    expires_at: datetime
    status: str = "active"  # "active", "expired", "rejected", "banned"
    boosted: bool = False
    reject_reason: str | None = Field(default=None)
    video_path: str | None = Field(default=None)  # Videó útvonala

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_session():
    with Session(engine) as session:
        yield session

class WishRequest(BaseModel):
    username: str
    phone: str
    wish: str

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        cleaned = re.sub(r'[\s\-()]', '', v)
        pattern = r"^(\+36|06)\d{9}$"
        if not re.match(pattern, cleaned):
            raise ValueError("Érvénytelen telefonszám formátum! Használd a +36301234567 vagy 06301234567 formátumot.")
        return cleaned

class RejectRequest(BaseModel):
    reason: str

def check_if_banned(username: str, phone: str, session: Session) -> bool:
    statement = select(User).where(
        User.username == username, 
        User.phone == phone, 
        User.status == "rejected"
    )
    rejected_wishes = session.exec(statement).all()
    return len(rejected_wishes) >= 3

def validate_and_update_phone_active(phone: str, username: str, session: Session):
    cleaned_phone = re.sub(r'[\s\-()]', '', phone)
    statement_phone_active = select(User).where(
        User.phone == cleaned_phone, 
        User.status == "active"
    ).order_by(User.id.desc())
    active_on_phone = session.exec(statement_phone_active).first()
    
    if active_on_phone:
        if datetime.now() < active_on_phone.expires_at:
            if active_on_phone.username != username:
                raise HTTPException(status_code=400, detail=f"Erről a telefonszámról már fut egy aktív paktum '{active_on_phone.username}' néven! Várj, amíg lejár.")
            else:
                raise HTTPException(status_code=400, detail="Még fut egy aktív paktumod ezen a készüléken!")
        else:
            active_on_phone.status = "expired"
            session.add(active_on_phone)
            session.commit()

def apply_chain_boost(session: Session):
    statement = select(User).where(User.status == "active").order_by(User.id.desc())
    active_users = session.exec(statement).all()
    
    if active_users:
        predecessor = active_users[0]
        if not predecessor.boosted:
            predecessor.expires_at = predecessor.expires_at + timedelta(hours=7)
            predecessor.boosted = True
            session.add(predecessor)

@app.post("/submit-wish")
def submit_wish(data: WishRequest, session: Session = Depends(get_session)):
    cleaned_phone = re.sub(r'[\s\-()]', '', data.phone)
    
    if check_if_banned(data.username, cleaned_phone, session):
        raise HTTPException(status_code=403, detail="Ezt az azonosítót és telefonszámot véglegesen letiltotta az Univerzum.")

    validate_and_update_phone_active(cleaned_phone, data.username, session)
    apply_chain_boost(session)

    now = datetime.now()
    expiry_time = now + timedelta(hours=24)
    
    db_user = User(
        username=data.username,
        phone=cleaned_phone,
        wish=data.wish,
        expires_at=expiry_time,
        status="active",
        boosted=False,
        reject_reason=None,
        video_path=None
    )
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    
    return {
        "message": "A paktum létrejött.",
        "expires_at": expiry_time.isoformat()
    }

@app.post("/submit-wish-video")
async def submit_wish_video(
    username: str = Form(...),
    phone: str = Form(...),
    video: UploadFile = File(...),
    session: Session = Depends(get_session)
):
    cleaned_phone = re.sub(r'[\s\-()]', '', phone)
    
    pattern = r"^(\+36|06)\d{9}$"
    if not re.match(pattern, cleaned_phone):
        raise HTTPException(status_code=400, detail="Érvénytelen telefonszám formátum!")

    if check_if_banned(username, cleaned_phone, session):
        raise HTTPException(status_code=403, detail="Ezt az azonosítót és telefonszámot véglegesen letiltotta az Univerzum.")

    validate_and_update_phone_active(cleaned_phone, username, session)
    apply_chain_boost(session)

    try:
        file_extension = video.filename.split(".")[-1] if "." in video.filename else "webm"
        safe_username = re.sub(r'[^a-zA-Z0-9_-]', '_', username)
        file_name = f"{safe_username}_{cleaned_phone}_{int(datetime.now().timestamp())}.{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, file_name)

        contents = await video.read()
        with open(file_path, "wb") as f:
            f.write(contents)

        now = datetime.now()
        expiry_time = now + timedelta(hours=24)

        db_user = User(
            username=username,
            phone=cleaned_phone,
            wish="[Videós Kívánság]",
            expires_at=expiry_time,
            status="active",
            boosted=False,
            reject_reason=None,
            video_path=file_path
        )
        session.add(db_user)
        session.commit()
        session.refresh(db_user)

        return {
            "message": "A videós paktum létrejött.",
            "expires_at": expiry_time.isoformat()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Hiba történt a videó feldolgozása közben: {str(e)}")

@app.get("/active-wishes")
def get_active_wishes(session: Session = Depends(get_session)):
    statement = select(User).where(User.status == "active").order_by(User.id.asc())
    users = session.exec(statement).all()
    
    now = datetime.now()
    result = []
    for u in users:
        if now > u.expires_at:
            u.status = "expired"
            session.add(u)
            session.commit()
            continue
            
        time_left = int((u.expires_at - now).total_seconds())
        result.append({
            "id": u.id,
            "username": u.username,
            "phone": u.phone,
            "wish": u.wish,
            "time_left_seconds": max(0, time_left),
            "boosted": u.boosted,
            "video_path": u.video_path
        })
    return result

# ==========================================
# ADMIN VÉGPONTOK (Moderáció & Hatósági mentés)
# ==========================================

@app.get("/admin/all-wishes")
def admin_get_all_wishes(session: Session = Depends(get_session)):
    statement = select(User).order_by(User.id.desc())
    users = session.exec(statement).all()
    
    result = []
    for u in users:
        result.append({
            "id": u.id,
            "username": u.username,
            "phone": u.phone,
            "wish": u.wish,
            "status": u.status,
            "expires_at": u.expires_at.isoformat() if u.expires_at else None,
            "reject_reason": u.reject_reason,
            "video_path": u.video_path
        })
    return result

@app.get("/admin/watch-video/{wish_id}")
def admin_watch_video(wish_id: int, session: Session = Depends(get_session)):
    user = session.get(User, wish_id)
    if not user or not user.video_path or not os.path.exists(user.video_path):
        raise HTTPException(status_code=404, detail="A videó nem található.")
    return FileResponse(user.video_path)

@app.post("/admin/archive/{wish_id}")
def admin_archive_evidence(wish_id: int, session: Session = Depends(get_session)):
    user = session.get(User, wish_id)
    if not user:
        raise HTTPException(status_code=404, detail="A paktum nem található.")
    
    archived_info = {
        "id": user.id,
        "username": user.username,
        "phone": user.phone,
        "wish": user.wish,
        "status": user.status,
        "expires_at": str(user.expires_at),
        "video_path": user.video_path
    }
    
    copied_video = None
    if user.video_path and os.path.exists(user.video_path):
        vid_filename = os.path.basename(user.video_path)
        copied_video = os.path.join(ARCHIVE_DIR, vid_filename)
        shutil.copy(user.video_path, copied_video)
        archived_info["archived_video_copy"] = copied_video

    return {"message": "Bizonyíték sikeresen archiválva a hatóságok számára!", "data": archived_info}

@app.post("/admin/reject/{wish_id}")
def reject_wish(wish_id: int, data: RejectRequest, session: Session = Depends(get_session)):
    user = session.get(User, wish_id)
    if not user:
        raise HTTPException(status_code=404, detail="A kívánság nem található.")
    
    user.status = "rejected"
    user.reject_reason = data.reason
    session.add(user)
    session.commit()
    return {"message": "Sikeresen elutasítva."}

@app.post("/admin/ban/{wish_id}")
def admin_ban_user(wish_id: int, session: Session = Depends(get_session)):
    user = session.get(User, wish_id)
    if not user:
        raise HTTPException(status_code=404, detail="A felhasználó nem található.")
    
    statement = select(User).where(User.username == user.username, User.phone == user.phone)
    matching_users = session.exec(statement).all()
    
    for m in matching_users:
        m.status = "banned"
        session.add(m)
    
    session.commit()
    return {"message": f"A(z) {user.username} ({user.phone}) páros véglegesen letiltva."}

@app.get("/check-user/{username}/{phone}")
def check_user_status(username: str, phone: str, session: Session = Depends(get_session)):
    cleaned_phone = re.sub(r'[\s\-()]', '', phone)
    
    if check_if_banned(username, cleaned_phone, session):
        return {"status": "banned"}

    statement = select(User).where(
        User.username == username, 
        User.phone == cleaned_phone
    ).order_by(User.id.desc())
    users = session.exec(statement).all()
    
    if not users:
        return {"status": "none"}
        
    latest = users[0]
    now = datetime.now()
    
    if latest.status == "active" and now > latest.expires_at:
        latest.status = "expired"
        session.add(latest)
        session.commit()
        
    return {
        "status": latest.status,
        "wish": latest.wish,
        "reject_reason": latest.reject_reason,
        "expires_at": latest.expires_at.isoformat() if latest.expires_at else None,
        "video_path": latest.video_path
    }