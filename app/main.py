from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import firms, routes, shortlist
from app.agent import go_no_go
from app.db import init_db
from app.seed import seed_firms


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_firms()
    yield


app = FastAPI(title="RFP Copilot", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(shortlist.router)
app.include_router(firms.router)
app.include_router(go_no_go.router)


@app.get("/health")
def health():
    return {"ok": True}
