from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import export, firms, routes, shortlist
from app.agent import capability, go_no_go, matrix, narrative
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
app.include_router(matrix.router)
app.include_router(narrative.router)
app.include_router(capability.router)
app.include_router(export.router)


@app.get("/health")
def health():
    return {"ok": True}
