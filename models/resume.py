from __future__ import annotations

from pydantic import BaseModel


class ResumeVersion(BaseModel):
    id: int
    resume_id: int
    resume_name: str
    version_no: int
    label: str
    original_filename: str
    mime_type: str = ""
    sha256: str
    storage_path: str
    extracted_text: str
    created_at: str
    notes: str = ""
    is_default: bool = False
