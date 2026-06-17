"""SQLite persistence.

A thin, typed data-access layer over the models. Each rich object is stored as JSON in a
``data`` column, with a few fields lifted into real columns for querying, dedupe, and indexing.
Variant statistics live in their own table so the optimization loop can increment them with a
cheap ``UPDATE`` instead of rewriting an entire sequence.

Uses only the standard library ``sqlite3`` — no extra dependency.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    Prospect,
    ProspectStatus,
    Reply,
    ReplyClass,
    SendEvent,
    Sequence,
    SequenceStatus,
    Tier,
    VariantStats,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  tier TEXT NOT NULL,
  fit_score REAL NOT NULL DEFAULT 0,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status);
CREATE INDEX IF NOT EXISTS idx_prospects_tier ON prospects(tier);

CREATE TABLE IF NOT EXISTS sequences (
  id TEXT PRIMARY KEY,
  prospect_id TEXT NOT NULL,
  status TEXT NOT NULL,
  model TEXT,
  data TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sequences_prospect ON sequences(prospect_id);

CREATE TABLE IF NOT EXISTS variants (
  id TEXT PRIMARY KEY,
  sequence_id TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  element TEXT NOT NULL,
  content TEXT NOT NULL,
  sends INTEGER NOT NULL DEFAULT 0,
  replies INTEGER NOT NULL DEFAULT 0,
  positive INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_variants_sequence ON variants(sequence_id);

CREATE TABLE IF NOT EXISTS send_events (
  id TEXT PRIMARY KEY,
  prospect_id TEXT NOT NULL,
  sequence_id TEXT,
  step_index INTEGER,
  type TEXT NOT NULL,
  channel TEXT NOT NULL,
  provider TEXT,
  occurred_at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_prospect ON send_events(prospect_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON send_events(type);

CREATE TABLE IF NOT EXISTS replies (
  id TEXT PRIMARY KEY,
  prospect_id TEXT NOT NULL,
  sequence_id TEXT,
  classification TEXT,
  confidence REAL,
  handled INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_replies_prospect ON replies(prospect_id);

CREATE TABLE IF NOT EXISTS suppression (
  email TEXT PRIMARY KEY,
  reason TEXT,
  added_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Connection wrapper plus CRUD for every model the pipeline persists."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ----------------------------------------------------------------- prospects

    def upsert_prospect(self, prospect: Prospect) -> None:
        """Insert or fully replace by stable id (created_at is preserved on the JSON object).

        Dedupe is guaranteed — re-ingesting the same email never duplicates a row — but this is a
        full replace, not a merge: passing a fresh, unscored prospect overwrites prior scoring.
        Callers that re-ingest leads should read the existing record first and merge (the lead
        loader does exactly this).
        """
        prospect.updated_at = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO prospects (id, email, status, tier, fit_score, data, created_at, updated_at)
            VALUES (:id, :email, :status, :tier, :fit_score, :data, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET
              email=excluded.email, status=excluded.status, tier=excluded.tier,
              fit_score=excluded.fit_score, data=excluded.data, updated_at=excluded.updated_at
            """,
            {
                "id": prospect.id,
                "email": prospect.email.lower(),
                "status": prospect.status.value,
                "tier": prospect.tier.value,
                "fit_score": prospect.fit_score,
                "data": prospect.model_dump_json(),
                "created_at": prospect.created_at.isoformat(),
                "updated_at": prospect.updated_at.isoformat(),
            },
        )
        self.conn.commit()

    def get_prospect(self, prospect_id: str) -> Prospect | None:
        row = self.conn.execute(
            "SELECT data FROM prospects WHERE id = ?", (prospect_id,)
        ).fetchone()
        return Prospect.model_validate_json(row["data"]) if row else None

    def get_prospect_by_email(self, email: str) -> Prospect | None:
        row = self.conn.execute(
            "SELECT data FROM prospects WHERE email = ?", (email.lower(),)
        ).fetchone()
        return Prospect.model_validate_json(row["data"]) if row else None

    def list_prospects(
        self,
        status: ProspectStatus | None = None,
        tier: Tier | None = None,
        limit: int | None = None,
    ) -> list[Prospect]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(tier.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT data FROM prospects {where} ORDER BY fit_score DESC, created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [Prospect.model_validate_json(r["data"]) for r in rows]

    def count_prospects(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM prospects").fetchone()["n"]

    # ----------------------------------------------------------------- sequences

    def save_sequence(self, sequence: Sequence) -> None:
        """Persist a sequence and mirror its variants into the variants table.

        Variant stats are never clobbered on re-save: only the copy/definition is updated, so
        accumulated send/reply tallies survive a re-draft.
        """
        self.conn.execute(
            """
            INSERT INTO sequences (id, prospect_id, status, model, data, created_at)
            VALUES (:id, :prospect_id, :status, :model, :data, :created_at)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status, model=excluded.model, data=excluded.data
            """,
            {
                "id": sequence.id,
                "prospect_id": sequence.prospect_id,
                "status": sequence.status.value,
                "model": sequence.model,
                "data": sequence.model_dump_json(),
                "created_at": sequence.created_at.isoformat(),
            },
        )
        for v in sequence.variants:
            self.conn.execute(
                """
                INSERT INTO variants (id, sequence_id, step_index, element, content, sends, replies, positive)
                VALUES (:id, :sequence_id, :step_index, :element, :content, :sends, :replies, :positive)
                ON CONFLICT(id) DO UPDATE SET
                  step_index=excluded.step_index, element=excluded.element, content=excluded.content
                """,
                {
                    "id": v.id,
                    "sequence_id": sequence.id,
                    "step_index": v.step_index,
                    "element": v.element.value,
                    "content": v.content,
                    "sends": v.stats.sends,
                    "replies": v.stats.replies,
                    "positive": v.stats.positive,
                },
            )
        self.conn.commit()

    def get_sequence(self, sequence_id: str) -> Sequence | None:
        row = self.conn.execute(
            "SELECT data FROM sequences WHERE id = ?", (sequence_id,)
        ).fetchone()
        if not row:
            return None
        seq = Sequence.model_validate_json(row["data"])
        self._overlay_variant_stats(seq)
        return seq

    def sequences_for_prospect(self, prospect_id: str) -> list[Sequence]:
        rows = self.conn.execute(
            "SELECT data FROM sequences WHERE prospect_id = ? ORDER BY created_at ASC",
            (prospect_id,),
        ).fetchall()
        seqs = [Sequence.model_validate_json(r["data"]) for r in rows]
        for seq in seqs:
            self._overlay_variant_stats(seq)
        return seqs

    def update_sequence_status(self, sequence_id: str, status: SequenceStatus) -> None:
        self.conn.execute(
            "UPDATE sequences SET status = ? WHERE id = ?", (status.value, sequence_id)
        )
        self.conn.commit()

    def _overlay_variant_stats(self, sequence: Sequence) -> None:
        """Replace each variant's in-JSON stats with the live tallies from the variants table."""
        rows = self.conn.execute(
            "SELECT id, sends, replies, positive FROM variants WHERE sequence_id = ?",
            (sequence.id,),
        ).fetchall()
        live = {r["id"]: r for r in rows}
        for v in sequence.variants:
            r = live.get(v.id)
            if r is not None:
                v.stats = VariantStats(sends=r["sends"], replies=r["replies"], positive=r["positive"])

    # ------------------------------------------------------------------ variants

    def record_variant_outcome(
        self, variant_id: str, *, sends: int = 0, replies: int = 0, positive: int = 0
    ) -> None:
        self.conn.execute(
            "UPDATE variants SET sends = sends + ?, replies = replies + ?, positive = positive + ? WHERE id = ?",
            (sends, replies, positive, variant_id),
        )
        self.conn.commit()

    def get_variant_stats(self, variant_id: str) -> VariantStats | None:
        row = self.conn.execute(
            "SELECT sends, replies, positive FROM variants WHERE id = ?", (variant_id,)
        ).fetchone()
        if not row:
            return None
        return VariantStats(sends=row["sends"], replies=row["replies"], positive=row["positive"])

    # -------------------------------------------------------------------- events

    def add_event(self, event: SendEvent) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO send_events
              (id, prospect_id, sequence_id, step_index, type, channel, provider, occurred_at, data)
            VALUES (:id, :prospect_id, :sequence_id, :step_index, :type, :channel, :provider, :occurred_at, :data)
            """,
            {
                "id": event.id,
                "prospect_id": event.prospect_id,
                "sequence_id": event.sequence_id,
                "step_index": event.step_index,
                "type": event.type.value,
                "channel": event.channel.value,
                "provider": event.provider,
                "occurred_at": event.occurred_at.isoformat(),
                "data": event.model_dump_json(),
            },
        )
        self.conn.commit()

    def events_for_prospect(self, prospect_id: str) -> list[SendEvent]:
        rows = self.conn.execute(
            "SELECT data FROM send_events WHERE prospect_id = ? ORDER BY occurred_at ASC",
            (prospect_id,),
        ).fetchall()
        return [SendEvent.model_validate_json(r["data"]) for r in rows]

    # -------------------------------------------------------------------- replies

    def add_reply(self, reply: Reply) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO replies
              (id, prospect_id, sequence_id, classification, confidence, handled, received_at, data)
            VALUES (:id, :prospect_id, :sequence_id, :classification, :confidence, :handled, :received_at, :data)
            """,
            {
                "id": reply.id,
                "prospect_id": reply.prospect_id,
                "sequence_id": reply.sequence_id,
                "classification": reply.classification.value if reply.classification else None,
                "confidence": reply.confidence,
                "handled": int(reply.handled),
                "received_at": reply.received_at.isoformat(),
                "data": reply.model_dump_json(),
            },
        )
        self.conn.commit()

    def update_reply_classification(
        self, reply_id: str, classification: ReplyClass, confidence: float | None = None
    ) -> None:
        reply = self.get_reply(reply_id)
        if reply is None:
            return
        reply.classification = classification
        reply.confidence = confidence
        self.add_reply(reply)

    def get_reply(self, reply_id: str) -> Reply | None:
        row = self.conn.execute("SELECT data FROM replies WHERE id = ?", (reply_id,)).fetchone()
        return Reply.model_validate_json(row["data"]) if row else None

    def list_replies(
        self, classification: ReplyClass | None = None, handled: bool | None = None
    ) -> list[Reply]:
        clauses, params = [], []
        if classification is not None:
            clauses.append("classification = ?")
            params.append(classification.value)
        if handled is not None:
            clauses.append("handled = ?")
            params.append(int(handled))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT data FROM replies {where} ORDER BY received_at DESC", params
        ).fetchall()
        return [Reply.model_validate_json(r["data"]) for r in rows]

    # ---------------------------------------------------------------- suppression

    def suppress(self, email: str, reason: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO suppression (email, reason, added_at) VALUES (?, ?, ?)",
            (email.strip().lower(), reason, _now_iso()),
        )
        self.conn.commit()

    def is_suppressed(self, email: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM suppression WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
        return row is not None

    def list_suppressed(self) -> list[str]:
        rows = self.conn.execute("SELECT email FROM suppression ORDER BY added_at ASC").fetchall()
        return [r["email"] for r in rows]

    # ----------------------------------------------------------------- lifecycle

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
