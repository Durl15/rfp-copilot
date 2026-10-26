from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import routes, shortlist
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="RFP Copilot", lifespan=lifespan)
app.include_router(routes.router)
app.include_router(shortlist.router)


@app.get("/health")
def health():
    return {"ok": True}
