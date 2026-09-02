from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import Column, Integer, String, Float, Boolean, select, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship
from passlib.context import CryptContext
from jose import jwt, JWTError, ExpiredSignatureError
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os

# ============================================
# 1. CONFIGURACIÓN DE ENTORNO
# ============================================
load_dotenv()

app = FastAPI(
    title="Prizefighter API",
    description="API para un sistema de boxeo generacional con autenticación y roles",
    version="1.0.0"
)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL no está definida en el archivo .env")

if "postgresql+asyncpg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

SECRET_KEY = os.getenv("SECRET_KEY", "mi_clave_secreta_super_segura_cambia_esto")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# ============================================
# 2. BASE DE DATOS
# ============================================
Base = declarative_base()
engine = create_async_engine(DATABASE_URL)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# ============================================
# 3. MODELOS SQLAlchemy
# ============================================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    disabled = Column(Boolean, default=False)
    role = Column(String(20), default="user")  # "user" o "admin"

class BoxeadorORM(Base):
    __tablename__ = "boxeadores"
    id = Column(Integer, primary_key=True)
    nombre = Column(String(50), nullable=False)
    peso = Column(Float)
    altura = Column(Float)
    estilo = Column(String(30), nullable=False)

class PeleaORM(Base):
    __tablename__ = "peleas"
    id = Column(Integer, primary_key=True)
    boxeador1_id = Column(Integer, ForeignKey("boxeadores.id"))
    boxeador2_id = Column(Integer, ForeignKey("boxeadores.id"))
    resultado = Column(String(30))
    fecha = Column(String(20))
    torneo_id = Column(Integer, ForeignKey("torneos.id"), nullable=True)

class TorneoORM(Base):
    __tablename__ = "torneos"
    id = Column(Integer, primary_key=True)
    nombre = Column(String(100), nullable=False)
    fecha = Column(String(20), nullable=False)
    ubicacion = Column(String(100), nullable=False)
    peleas = relationship("PeleaORM", backref="torneo")

# ============================================
# 4. MODELOS PYDANTIC (Validación)
# ============================================
class BoxeadorPydantic(BaseModel):
    nombre: str = Field(min_length=2, max_length=30)
    peso: float = Field(gt=0, le=150)
    altura: float = Field(gt=0, le=250)
    estilo: str = Field(min_length=3, max_length=20)

    @field_validator("nombre")
    def validar_nombre(cls, v: str) -> str:
        if any(c.isdigit() for c in v):
            raise ValueError("El nombre no puede contener números")
        return v

class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class TorneoCreate(BaseModel):
    nombre: str = Field(min_length=3, max_length=100)
    fecha: str = Field(min_length=3, max_length=20)
    ubicacion: str = Field(min_length=3, max_length=100)

# ============================================
# 5. HASHING DE CONTRASEÑAS
# ============================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

# ============================================
# 6. JWT
# ============================================
def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

# ============================================
# 7. OAUTH2
# ============================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# ============================================
# 8. DEPENDENCIAS DE AUTENTICACIÓN Y ROLES
# ============================================
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar el token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception

    return user

async def require_admin(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Acceso no autorizado")
    return current_user

# ============================================
# 9. ENDPOINTS PÚBLICOS
# ============================================
@app.post("/register", status_code=201)
async def register(user_data: UserCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == user_data.username))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Usuario ya registrado")

    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="Email ya registrado")

    hashed = hash_password(user_data.password)
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed,
        role="user"
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return {
        "id": new_user.id,
        "username": new_user.username,
        "email": new_user.email,
        "role": new_user.role
    }

@app.post("/token")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(User).where(User.username == form_data.username))
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        data={"sub": user.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}

# ============================================
# 10. ENDPOINTS DE BOXEADORES (CRUD con BD)
# ============================================
@app.post("/boxeadores/", status_code=201)
async def crear_boxeador(
    boxeador: BoxeadorPydantic,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)  # Solo admin
):
    nuevo = BoxeadorORM(
        nombre=boxeador.nombre,
        peso=boxeador.peso,
        altura=boxeador.altura,
        estilo=boxeador.estilo
    )
    db.add(nuevo)
    await db.commit()
    await db.refresh(nuevo)
    return {
        "id": nuevo.id,
        "nombre": nuevo.nombre,
        "peso": nuevo.peso,
        "altura": nuevo.altura,
        "estilo": nuevo.estilo
    }

@app.get("/boxeadores/")
async def listar_boxeadores(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BoxeadorORM))
    return result.scalars().all()

@app.get("/boxeadores/{id}")
async def obtener_boxeador(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BoxeadorORM).where(BoxeadorORM.id == id))
    boxeador = result.scalars().first()
    if not boxeador:
        raise HTTPException(status_code=404, detail="Boxeador no encontrado")
    return boxeador

@app.put("/boxeadores/{id}")
async def actualizar_boxeador(
    id: int,
    boxeador: BoxeadorPydantic,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)  # Solo admin
):
    result = await db.execute(select(BoxeadorORM).where(BoxeadorORM.id == id))
    existente = result.scalars().first()
    if not existente:
        raise HTTPException(status_code=404, detail="Boxeador no encontrado")
    existente.nombre = boxeador.nombre
    existente.peso = boxeador.peso
    existente.altura = boxeador.altura
    existente.estilo = boxeador.estilo
    await db.commit()
    await db.refresh(existente)
    return existente

@app.delete("/boxeadores/{id}", status_code=204)
async def eliminar_boxeador(
    id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)  # Solo admin
):
    result = await db.execute(select(BoxeadorORM).where(BoxeadorORM.id == id))
    boxeador = result.scalars().first()
    if not boxeador:
        raise HTTPException(status_code=404, detail="Boxeador no encontrado")
    await db.delete(boxeador)
    await db.commit()
    return

# ============================================
# 11. ENDPOINTS DE PELEAS
# ============================================
@app.get("/peleas/")
async def listar_peleas(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(PeleaORM))
    return result.scalars().all()

# ============================================
# 12. ENDPOINTS DE TORNEOS (Solo admin)
# ============================================
@app.post("/torneos/", status_code=201)
async def crear_torneo(
    torneo: TorneoCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    nuevo_torneo = TorneoORM(
        nombre=torneo.nombre,
        fecha=torneo.fecha,
        ubicacion=torneo.ubicacion
    )
    db.add(nuevo_torneo)
    await db.commit()
    await db.refresh(nuevo_torneo)
    return {
        "id": nuevo_torneo.id,
        "nombre": nuevo_torneo.nombre,
        "fecha": nuevo_torneo.fecha,
        "ubicacion": nuevo_torneo.ubicacion
    }

# ============================================
# 13. ENDPOINT PROTEGIDO (Usuario autenticado)
# ============================================
@app.get("/entrenadores/me")
async def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "role": current_user.role
    }

# ============================================
# 14. HEALTH CHECK
# ============================================
@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "Prizefighter API is running"}
