import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, model_validator

from app import ingest
from app.llm.meta_client import MetaClient

router = APIRouter()

MAX_PDF_BYTES = 25 * 1024 * 1024


class SolicitationIn(BaseModel):
    text: str | None = None
    sam_notice_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.text and self.text.strip()) == bool(self.sam_notice_id):
            raise ValueError("provide exactly one of: text, sam_notice_id (or upload a PDF as multipart 'file')")
        return self


def get_llm() -> MetaClient:
    return MetaClient()


def _public(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "raw_text"}
    out["llm_filled"] = json.loads(out["llm_filled"])
    out["text_chars"] = len(row["raw_text"])
    return out


@router.post("/solicitations", status_code=201)
async def create_solicitation(request: Request):
    llm = get_llm()
    ctype = request.headers.get("content-type", "")
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(422, "multipart body must include a 'file' field")
            data = await upload.read()
            if not data.startswith(b"%PDF"):
                raise HTTPException(415, "file is not a PDF")
            if len(data) > MAX_PDF_BYTES:
                raise HTTPException(413, "PDF exceeds 25 MB")
            row = await run_in_threadpool(ingest.ingest_pdf, data, upload.filename or "upload.pdf", llm)
        else:
            try:
                body = SolicitationIn.model_validate(await request.json())
            except Exception as exc:
                raise HTTPException(422, str(exc))
            if body.text and body.text.strip():
                row = await run_in_threadpool(ingest.ingest_text, body.text, llm)
            else:
                row = await run_in_threadpool(ingest.ingest_sam, body.sam_notice_id, llm)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _public(row)


@router.get("/solicitations/{sol_id}")
def read_solicitation(sol_id: str):
    row = ingest.get_solicitation(sol_id)
    if not row:
        raise HTTPException(404, "not found")
    return _public(row)
