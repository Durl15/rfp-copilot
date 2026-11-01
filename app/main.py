from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import console, demo, export, firms, routes, shortlist, storage, voice
from app.agent import capability, go_no_go, matrix, narrative, run
from app.config import get_settings
from app.db import init_db
from app.seed import seed_firms


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.download_if_present()  # before migrations, so the restored file is the one that gets migrated and seeded
    init_db()
    seed_firms()
    yield
    storage.flush()


app = FastAPI(title="RFP Copilot", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(shortlist.router)
app.include_router(firms.router)
app.include_router(go_no_go.router)
app.include_router(matrix.router)
app.include_router(narrative.router)
app.include_router(capability.router)
app.include_router(export.router)
app.include_router(run.router)
app.include_router(demo.router)
app.include_router(voice.router)
app.include_router(console.router)
app.mount("/static", StaticFiles(directory=console.STATIC_DIR), name="static")


@app.get("/health")
def health():
    s = get_settings()
    return {"ok": True, "model": s.meta_model, "api_base": s.meta_api_base}
