"""Read uploaded context files and distill them into a structured :class:`Offer` brief.

The engine learns your product from files you drop in the ``context/`` folder — portfolio,
case studies, pricing, finishes, process notes — rather than by asking questions in chat. The
LLM call is deliberately isolated from the file reading and response parsing so those parts can
be tested without a live request, and so the distillation never invents facts the files don't
support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import Offer

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".markdown", ".txt"}
MAX_CONTEXT_CHARS = 120_000

SYSTEM_PROMPT = (
    "You are a precise B2B positioning analyst. You read a company's own materials and distill a "
    "structured brief used to write cold outreach. Use only what the materials actually support. "
    "Never invent facts, numbers, clients, awards, or claims. If something is not in the materials, "
    "leave that field empty rather than guessing. Return a single JSON object and nothing else."
)

OFFER_SCHEMA_HINT = """Return a JSON object with exactly these keys:
{
  "name": string,                 // business or offering name; "" if unclear
  "summary": string,              // 1-3 sentences: what they sell and to whom
  "value_props": [string],        // concrete benefits in the buyer's language
  "proof_points": [string],       // craft, materials, experience, past work — only if stated
  "buyer_motivations": [string],  // why a fitting buyer would care
  "objections": [{"objection": string, "response": string}],
  "price_posture": string,        // e.g. "bespoke, premium, project-based" — only if inferable
  "icp_hypotheses": [string]      // who this is for, per the materials
}"""


@dataclass
class ContextDoc:
    filename: str
    text: str


def read_context_files(context_dir: Path) -> list[ContextDoc]:
    """Read and extract text from every supported file in the context directory."""
    docs: list[ContextDoc] = []
    if not context_dir.exists():
        return docs
    for path in sorted(context_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        text = _extract_text(path)
        if text.strip():
            docs.append(ContextDoc(filename=path.name, text=text.strip()))
    return docs


def _extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    return ""


def build_prompt(docs: list[ContextDoc]) -> str:
    """Concatenate the materials (capped to a token budget) and append the schema instruction."""
    parts = ["Here are the company's own materials:\n"]
    used = 0
    for doc in docs:
        chunk = f"\n===== FILE: {doc.filename} =====\n{doc.text}\n"
        if used + len(chunk) > MAX_CONTEXT_CHARS:
            chunk = chunk[: max(0, MAX_CONTEXT_CHARS - used)]
        parts.append(chunk)
        used += len(chunk)
        if used >= MAX_CONTEXT_CHARS:
            break
    parts.append("\n\n" + OFFER_SCHEMA_HINT)
    return "".join(parts)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def parse_offer_response(text: str, source_files: list[str]) -> Offer:
    """Parse the model's JSON into a validated Offer, tolerating code fences."""
    payload = json.loads(_strip_code_fences(text))
    payload["source_files"] = source_files
    return Offer.model_validate(payload)


def _call_model(client, model: str, system: str, prompt: str, max_tokens: int = 2000) -> str:
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")


def distill_offer(docs: list[ContextDoc], *, model: str, api_key: str, client=None) -> Offer:
    """Distill the read documents into an Offer via the model (client is injectable for tests)."""
    if client is None:
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
    raw = _call_model(client, model, SYSTEM_PROMPT, build_prompt(docs))
    return parse_offer_response(raw, [d.filename for d in docs])


def offer_path(settings: Settings) -> Path:
    return settings.paths.data_dir / "offer_brief.json"


def save_offer(offer: Offer, settings: Settings) -> Path:
    settings.paths.ensure()
    path = offer_path(settings)
    path.write_text(offer.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_offer(settings: Settings) -> Offer | None:
    path = offer_path(settings)
    if not path.exists():
        return None
    return Offer.model_validate_json(path.read_text(encoding="utf-8"))


def ingest(settings: Settings, docs: list[ContextDoc] | None = None, client=None) -> Offer:
    """Read context (if not supplied), distill the offer brief, persist it, and return it."""
    settings.paths.ensure()
    if docs is None:
        docs = read_context_files(settings.paths.context_dir)
    if not docs:
        raise FileNotFoundError(
            f"No readable context files in {settings.paths.context_dir}. "
            "Add .pdf/.docx/.md/.txt files describing your offer."
        )
    api_key = settings.require_api_key()
    offer = distill_offer(docs, model=settings.model, api_key=api_key, client=client)
    save_offer(offer, settings)
    return offer
