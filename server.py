from typing import List, Optional

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


app = FastAPI(title="Emma Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/ui", StaticFiles(directory="ui"), name="ui")

security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminUserCreate(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "user"


class AdminUserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class AdminPasswordReset(BaseModel):
    password: str


class ConversationCreate(BaseModel):
    title: str = "New chat"
    model: str


class ConversationTitleUpdate(BaseModel):
    title: str


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: bool = True
    keep_alive: Optional[str] = None
    conversation_id: Optional[str] = None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    pass


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    pass


@app.on_event("startup")
async def startup():
    pass


@app.post("/auth/login")
async def login(body: LoginRequest):
    pass


@app.post("/auth/logout")
async def logout(
    user: dict = Depends(get_current_user),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    pass


@app.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    pass


@app.get("/admin/users")
async def admin_list_users(user: dict = Depends(get_current_user)):
    pass


@app.post("/admin/users")
async def admin_create_user(
    body: AdminUserCreate,
    user: dict = Depends(get_current_user),
):
    pass


@app.patch("/admin/users/{target_user_id}")
async def admin_update_user(
    target_user_id: int,
    body: AdminUserUpdate,
    user: dict = Depends(get_current_user),
):
    pass


@app.post("/admin/users/{target_user_id}/reset-password")
async def admin_reset_password(
    target_user_id: int,
    body: AdminPasswordReset,
    user: dict = Depends(get_current_user),
):
    pass


@app.delete("/admin/users/{target_user_id}")
async def admin_delete_user(
    target_user_id: int,
    user: dict = Depends(get_current_user),
):
    pass


@app.get("/health")
async def health(user: dict = Depends(get_current_user)):
    pass


@app.get("/files")
async def list_files(user: dict = Depends(get_current_user)):
    pass


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    scope: str = "user",
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    pass


@app.delete("/files/{scope}/{stem}")
async def delete_file(
    scope: str,
    stem: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    pass


@app.delete("/files/{scope}")
async def delete_all_files(
    scope: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    pass


@app.get("/files/{scope}/{stem}/download")
async def download_file(
    scope: str,
    stem: str,
    owner_id: Optional[int] = Query(default=None),
    user: dict = Depends(get_current_user),
):
    pass


@app.get("/conversations")
async def list_conversations(user: dict = Depends(get_current_user)):
    pass


@app.post("/conversations")
async def create_conversation(
    body: ConversationCreate,
    user: dict = Depends(get_current_user),
):
    pass


@app.get("/conversations/{conv_id}")
async def get_conversation(
    conv_id: str,
    user: dict = Depends(get_current_user),
):
    pass


@app.patch("/conversations/{conv_id}/title")
async def update_conversation_title(
    conv_id: str,
    body: ConversationTitleUpdate,
    user: dict = Depends(get_current_user),
):
    pass


@app.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    user: dict = Depends(get_current_user),
):
    pass


@app.post("/chat")
async def chat(
    req: ChatRequest,
    user: dict = Depends(get_current_user),
):
    pass
