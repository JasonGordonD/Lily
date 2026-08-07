"""Shared in-memory fake of the supabase-py query builder, sized for the
arsenal tests (WO-LILY-ARSENAL-SEED-001).

The PATCH-003 arsenal tests carry their own smaller fake supporting
select/eq/limit/insert. The seeding work needs update(), order(), partial
unique indexes and storage, so this fake mirrors more of the live schema's
BEHAVIOUR — specifically the two constraints the design leans on:

  UNIQUE(arsenal_id, group_id) on the usage table
      a second serve of one entry to the same group RAISES, which is what
      makes "no group ever sees the same entry twice" structural.

  UNIQUE(partition) WHERE status='running' on the runs table
      a second concurrent seeding run RAISES, which is what makes
      "two runs must not double-fill" structural.

A fake that silently accepted both would let the tests pass while the real
guarantees were untested, so both are enforced here.
"""

import itertools
import uuid


class FakeResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._filters = []
        self._count = False
        self._limit = None
        self._op = None
        self._payload = None
        self._order = None
        self._desc = False

    # -- builder ---------------------------------------------------------
    def select(self, _cols="*", count=None):
        self._op = "select"
        self._count = count == "exact"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    # -- execution -------------------------------------------------------
    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            payload = self._payload
            items = payload if isinstance(payload, list) else [payload]
            created = []
            for item in items:
                row = dict(item)
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault(
                    "created_at",
                    f"2026-08-07T00:00:{next(self._db.clock):02d}+00:00",
                )
                self._db.apply_defaults(self._table, row)
                self._db.enforce_constraints(self._table, row)
                rows.append(row)
                created.append(row)
            return FakeResult(created)

        matched = [r for r in rows if self._matches(r)]

        if self._op == "update":
            for row in matched:
                for key, value in (self._payload or {}).items():
                    # "now()" is a Postgres literal in the production code;
                    # the fake resolves it to a stable timestamp.
                    row[key] = self._db.now if value == "now()" else value
            return FakeResult(list(matched))

        if self._order:
            matched = sorted(
                matched, key=lambda r: str(r.get(self._order) or ""),
                reverse=self._desc,
            )
        if self._limit is not None:
            matched = matched[: self._limit]
        return FakeResult(
            list(matched), count=len(matched) if self._count else None
        )


class FakeStorageBucket:
    def __init__(self, db, bucket):
        self._db = db
        self._bucket = bucket

    def upload(self, path, data, options=None):
        store = self._db._files.setdefault(self._bucket, {})
        if path in store:
            raise Exception("Duplicate: resource already exists")
        store[path] = data
        return {"path": path}

    def create_signed_url(self, path, ttl):
        if path not in self._db._files.get(self._bucket, {}):
            raise Exception(f"Object not found: {path}")
        return {"signedURL": f"https://fake.storage/{self._bucket}/{path}?ttl={ttl}"}

    def get_public_url(self, path):
        return f"https://fake.storage/{self._bucket}/{path}"


class FakeStorage:
    def __init__(self, db):
        self._db = db

    def from_(self, bucket):
        return FakeStorageBucket(self._db, bucket)


class FakeSupabase:
    """Minimal supabase client double with real constraint behaviour."""

    def __init__(self):
        self.tables = {}
        self.clock = itertools.count(1)
        self.now = "2026-08-07T12:00:00+00:00"
        # Bucket -> {path: bytes}. Named with a leading underscore so the
        # public `storage` attribute can be the client surface the
        # production code actually calls (supabase.storage.from_(...)).
        self._files = {}

    def table(self, name):
        return FakeQuery(self, name)

    @property
    def storage(self):
        return FakeStorage(self)

    def apply_defaults(self, table, row):
        """Column defaults the real schema fills in. `started_at` and
        `heartbeat_at` default to now() on the runs table, and modelling
        that matters: a fake that left heartbeat_at null would make every
        run look unparseably old, so the stale-reclaim would clear a LIVE
        run's row and the concurrency guard would appear broken (or worse,
        appear to work while silently letting two runs fill one shelf)."""
        if table == "lily_picture_arsenal_runs":
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            row.setdefault("started_at", now)
            row.setdefault("heartbeat_at", now)
            for counter in (
                "created_count", "skipped_duplicate", "rejected_moderation",
                "rejected_gate", "error_count",
            ):
                row.setdefault(counter, 0)
            row.setdefault("cost_usd", 0)
        if table == "lily_picture_arsenal":
            row.setdefault("status", "ready")
            row.setdefault("generation_model", "grok-imagine-image")

    def enforce_constraints(self, table, row):
        """The two uniqueness guarantees the arsenal design depends on."""
        existing = self.tables.setdefault(table, [])
        if table == "lily_picture_arsenal_usage":
            for other in existing:
                if (
                    other.get("arsenal_id") == row.get("arsenal_id")
                    and other.get("group_id") == row.get("group_id")
                ):
                    raise Exception(
                        "duplicate key value violates unique constraint "
                        '"lily_picture_arsenal_usage_arsenal_id_group_id_key"'
                    )
        if table == "lily_picture_arsenal_runs" and row.get("status") == "running":
            for other in existing:
                if (
                    other.get("partition") == row.get("partition")
                    and other.get("status") == "running"
                ):
                    raise Exception(
                        "duplicate key value violates unique constraint "
                        '"lily_picture_arsenal_runs_one_active_idx"'
                    )


def seed_entry(db, partition, **overrides):
    """Insert one ready arsenal row and return it."""
    row = {
        "partition": partition,
        "question_text": overrides.pop("question_text", "what is this object"),
        "canonical_answer": overrides.pop("canonical_answer", "a teapot"),
        "acceptable_answers": ["a teapot", "teapot"],
        "generation_prompt": "a teapot on a table",
        "generation_model": "grok-imagine-image",
        "image_storage_path": f"{partition}/abc123.jpg",
        "image_source": "generated",
        "status": "ready",
        "format": "identify",
        "binding_direction": "image_first",
        "difficulty_tier": 2,
        "subject_area": "objects",
    }
    row.update(overrides)
    return db.table("lily_picture_arsenal").insert(row).execute().data[0]
