"""Persistent order intent and execution lifecycle, with no network or signing.

An executor must commit SUBMITTING *before* handing bytes to a transport. A
transport timeout means UNCERTAIN, never a fill or permission to send again.
Confirmed evidence must come from the executor's reconciliation adapter; this
module validates its shape and ordering, not the external chain's truth.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


STATES = frozenset({"CREATED", "VALIDATING", "QUOTED", "SIMULATED", "PREPARED", "SUBMITTING",
                    "SUBMITTED", "UNCERTAIN", "CONFIRMED", "FAILED", "EXPIRED", "CANCELLED"})
TERMINAL_STATES = frozenset({"CONFIRMED", "FAILED", "EXPIRED", "CANCELLED"})
RECONCILIATION_STATES = frozenset({"SUBMITTING", "SUBMITTED", "UNCERTAIN"})
ALLOWED_TRANSITIONS = {
    "CREATED": {"VALIDATING", "QUOTED", "FAILED", "CANCELLED"},
    "VALIDATING": {"QUOTED", "FAILED", "CANCELLED"},
    "QUOTED": {"SIMULATED", "FAILED", "EXPIRED", "CANCELLED"},
    "SIMULATED": {"PREPARED", "CONFIRMED", "FAILED", "EXPIRED", "CANCELLED"},
    "PREPARED": {"SUBMITTING", "FAILED", "EXPIRED", "CANCELLED"},
    "SUBMITTING": {"SUBMITTED", "UNCERTAIN", "FAILED"},
    "SUBMITTED": {"CONFIRMED", "UNCERTAIN", "FAILED"},
    "UNCERTAIN": {"CONFIRMED", "FAILED"},
}
_FORBIDDEN_KEYS = frozenset({"privatekey", "walletprivatekey", "secretkey", "mnemonic",
                           "seed", "seedphrase", "keypair", "authorization", "apikey",
                           "accesstoken", "refreshtoken"})


class OrderError(ValueError):
    pass


class IdempotencyConflict(OrderError):
    pass


class ConcurrentOrderUpdate(OrderError):
    pass


class IllegalTransition(OrderError):
    pass


class OrderExpired(OrderError):
    """The EXPIRED transition has already been committed when this is raised."""
    def __init__(self, order):
        self.order = order
        super().__init__(f"Order {order['id']} expired before submission")


def _integer(value, name, minimum=0, maximum=(1 << 64) - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise OrderError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _text(value, name, maximum=256):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise OrderError(f"{name} must be a nonempty, trimmed string up to {maximum} characters")
    if any(not char.isprintable() for char in value):
        raise OrderError(f"{name} contains control characters")
    return value


def _timestamp(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise OrderError(f"{name} must be a finite nonnegative timestamp")
    return float(value)


def _check_secret_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise OrderError("Evidence keys must be strings")
            normal = "".join(char.lower() for char in key if char.isalnum())
            if normal in _FORBIDDEN_KEYS:
                raise OrderError(f"Secret-bearing evidence field is forbidden: {key}")
            _check_secret_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_secret_keys(child)


def _json(value):
    try:
        _check_secret_keys(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise OrderError(f"Evidence is not safe canonical JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise OrderError("Evidence exceeds 1 MiB")
    return encoded


def _digest(encoded):
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OrderIntent:
    idempotency_key: str
    mode: str
    side: str
    input_mint: str
    output_mint: str
    amount_raw: int
    slippage_bps: int
    strategy_version: str
    position_id: int | None = None

    def __post_init__(self):
        for field in ("idempotency_key", "strategy_version", "input_mint", "output_mint"):
            _text(getattr(self, field), field, 128)
        if self.mode not in {"PAPER", "LIVE"} or self.side not in {"BUY", "SELL"}:
            raise OrderError("mode must be PAPER/LIVE and side BUY/SELL")
        if self.input_mint == self.output_mint:
            raise OrderError("Input and output mint must differ")
        _integer(self.amount_raw, "amount_raw", 1)
        _integer(self.slippage_bps, "slippage_bps", 0, 10000)
        if self.position_id is not None:
            _integer(self.position_id, "position_id", 1, (1 << 63) - 1)


class OrderStore:
    """Serialize short SQLite mutations; no request/signing work runs in a lock."""
    def __init__(self, db_path, clock=time.time):
        self.db_path = str(Path(db_path).resolve())
        self.clock = clock
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_orders (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    intent_json TEXT NOT NULL,
                    intent_hash TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('PAPER','LIVE')),
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    quote_json TEXT,
                    quote_hash TEXT,
                    expires_at REAL,
                    last_valid_block_height INTEGER,
                    transaction_id TEXT UNIQUE,
                    signed_message_hash TEXT,
                    confirmed_simulated INTEGER,
                    confirmation_json TEXT
                );
                CREATE TABLE IF NOT EXISTS execution_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES execution_orders(id),
                    version INTEGER NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    previous_event_hash TEXT,
                    event_hash TEXT NOT NULL,
                    UNIQUE(order_id,version)
                );
                CREATE INDEX IF NOT EXISTS execution_orders_reconciliation ON execution_orders(state,mode);
                CREATE TABLE IF NOT EXISTS execution_order_postings (
                    order_id TEXT PRIMARY KEY REFERENCES execution_orders(id),
                    timestamp REAL NOT NULL,
                    result_json TEXT NOT NULL,
                    result_hash TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS execution_postings_no_update
                    BEFORE UPDATE ON execution_order_postings BEGIN
                    SELECT RAISE(ABORT,'Order postings are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_postings_no_delete
                    BEFORE DELETE ON execution_order_postings BEGIN
                    SELECT RAISE(ABORT,'Order postings are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS execution_events_no_update
                    BEFORE UPDATE ON execution_order_events BEGIN
                    SELECT RAISE(ABORT,'Order events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS execution_events_no_delete
                    BEFORE DELETE ON execution_order_events BEGIN
                    SELECT RAISE(ABORT,'Order events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS execution_intent_immutable
                    BEFORE UPDATE OF id,idempotency_key,intent_json,intent_hash,mode,created_at ON execution_orders
                    BEGIN SELECT RAISE(ABORT,'Order intent is immutable'); END;
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self):
        return _timestamp(self.clock(), "clock")

    @staticmethod
    def _record(row):
        if row is None:
            raise OrderError("Unknown order")
        result = dict(row)
        for source, target in (("intent_json", "intent"), ("quote_json", "quote"), ("confirmation_json", "confirmation")):
            encoded = result.pop(source)
            result[target] = json.loads(encoded) if encoded is not None else None
        if result["confirmed_simulated"] is not None:
            result["confirmed_simulated"] = bool(result["confirmed_simulated"])
        return result

    @staticmethod
    def _load(conn, order_id):
        row = conn.execute("SELECT * FROM execution_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise OrderError("Unknown order")
        return row

    def get(self, order_id):
        with self._connection() as conn:
            return self._record(self._load(conn, order_id))

    def _event(self, conn, order_id, version, from_state, to_state, timestamp, evidence):
        encoded = _json(evidence)
        previous = conn.execute("SELECT event_hash FROM execution_order_events WHERE order_id=? ORDER BY version DESC LIMIT 1", (order_id,)).fetchone()
        previous_hash = previous[0] if previous else None
        evidence_hash = _digest(encoded)
        event_hash = _digest(_json({"order_id": order_id, "version": version, "from_state": from_state,
                                   "to_state": to_state, "timestamp": timestamp,
                                   "evidence_hash": evidence_hash, "previous_event_hash": previous_hash}))
        conn.execute("""INSERT INTO execution_order_events
            (order_id,version,from_state,to_state,timestamp,evidence_json,evidence_hash,previous_event_hash,event_hash)
            VALUES(?,?,?,?,?,?,?,?,?)""", (order_id, version, from_state, to_state, timestamp,
                                           encoded, evidence_hash, previous_hash, event_hash))

    def create(self, intent):
        if not isinstance(intent, OrderIntent):
            raise OrderError("create requires an immutable OrderIntent")
        encoded = _json(asdict(intent))
        intent_hash = _digest(encoded)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM execution_orders WHERE idempotency_key=?", (intent.idempotency_key,)).fetchone()
            if existing is not None:
                if existing["intent_json"] != encoded:
                    raise IdempotencyConflict("Idempotency key belongs to a different immutable intent")
                conn.commit()
                return self._record(existing)
            order_id, now = uuid.uuid4().hex, self._now()
            conn.execute("""INSERT INTO execution_orders
                (id,idempotency_key,intent_json,intent_hash,mode,state,version,created_at,updated_at)
                VALUES(?,?,?,?,?,'CREATED',0,?,?)""",
                (order_id, intent.idempotency_key, encoded, intent_hash, intent.mode, now, now))
            self._event(conn, order_id, 0, None, "CREATED", now, {"intent_hash": intent_hash})
            row = self._load(conn, order_id)
            conn.commit()
            return self._record(row)

    @staticmethod
    def _expired(row, now, current_block_height):
        return ((row["expires_at"] is not None and now >= row["expires_at"]) or
                (current_block_height is not None and row["last_valid_block_height"] is not None
                 and current_block_height > row["last_valid_block_height"]))

    def _change(self, conn, row, target, evidence, now, updates=None):
        updates = dict(updates or {})
        updates.update(state=target, version=row["version"] + 1, updated_at=now)
        # Column names are internal constants, never supplied by a caller.
        assignments = ",".join(f"{key}=?" for key in updates)
        changed = conn.execute(f"UPDATE execution_orders SET {assignments} WHERE id=? AND version=?",
                               (*updates.values(), row["id"], row["version"]))
        if changed.rowcount != 1:
            raise ConcurrentOrderUpdate("Order changed during transition")
        self._event(conn, row["id"], row["version"] + 1, row["state"], target, now, evidence)
        return self._load(conn, row["id"])

    @staticmethod
    def _confirmation(row, evidence):
        intent = json.loads(row["intent_json"])
        _integer(evidence.get("input_spent_raw"), "input_spent_raw", 1)
        _integer(evidence.get("output_received_raw"), "output_received_raw", 1)
        _integer(evidence.get("fee_paid_raw"), "fee_paid_raw", 0)
        if evidence["input_spent_raw"] != intent["amount_raw"]:
            raise OrderError("Confirmed exact-input quantity differs from immutable intent")
        if row["mode"] == "PAPER":
            if (row["state"] != "SIMULATED" or evidence.get("execution") != "PAPER" or
                    evidence.get("simulated") is not True or evidence.get("actual_money_used") is not False):
                raise OrderError("Paper confirmation requires explicit simulated evidence and no actual money")
            return {"confirmed_simulated": 1, "confirmation_json": _json(evidence)}
        if (row["state"] not in {"SUBMITTED", "UNCERTAIN"} or evidence.get("execution") != "LIVE" or
                evidence.get("transaction_id") != row["transaction_id"] or
                evidence.get("confirmation_status") not in {"confirmed", "finalized"} or
                evidence.get("success") is not True or evidence.get("reconciled") is not True or
                evidence.get("simulated") is not False or evidence.get("actual_money_used") is not True):
            raise OrderError("Live confirmation requires matching, successful, reconciled chain evidence")
        return {"confirmed_simulated": 0, "confirmation_json": _json(evidence)}

    def transition(self, order_id, to_state, *, expected_version, evidence=None,
                   expires_at=None, last_valid_block_height=None, current_block_height=None,
                   apply_confirmation=None, apply_failure=None):
        """Atomically change state after an executor supplies supporting evidence.

        `expires_at` and `last_valid_block_height` may only be set with QUOTED.
        A pre-submission expiry is committed before OrderExpired is raised.
        Never catch OrderExpired and broadcast the prepared transaction anyway.
        """
        _integer(expected_version, "expected_version", 0, (1 << 63) - 1)
        if to_state not in STATES:
            raise IllegalTransition("Unknown target state")
        if apply_confirmation is not None and (to_state != "CONFIRMED" or not callable(apply_confirmation)):
            raise OrderError("A confirmation applier is only allowed on CONFIRMED")
        if apply_failure is not None and (to_state != "FAILED" or not callable(apply_failure)):
            raise OrderError("A failed-fee applier is only allowed on reconciled FAILED")
        if not isinstance(evidence, dict) or not evidence:
            raise OrderError("A nonempty evidence object is required")
        _json(evidence)
        if current_block_height is not None:
            _integer(current_block_height, "current_block_height", 0, (1 << 63) - 1)
        if expires_at is not None:
            _timestamp(expires_at, "expires_at")
        if last_valid_block_height is not None:
            _integer(last_valid_block_height, "last_valid_block_height", 0, (1 << 63) - 1)
        if to_state != "QUOTED" and (expires_at is not None or last_valid_block_height is not None):
            raise OrderError("Expiry is immutable after the quote is recorded")
        expired = False
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load(conn, order_id)
            if row["version"] != expected_version:
                raise ConcurrentOrderUpdate("Expected version does not match persisted order")
            if to_state not in ALLOWED_TRANSITIONS.get(row["state"], set()):
                raise IllegalTransition(f"Cannot move {row['state']} to {to_state}")
            now = self._now()
            if (row["state"] in {"QUOTED", "SIMULATED", "PREPARED"} and
                    to_state not in {"CANCELLED", "FAILED", "EXPIRED"} and
                    self._expired(row, now, current_block_height)):
                row = self._change(conn, row, "EXPIRED", {"reason": "Quote validity expired before submission",
                                   "current_block_height": current_block_height}, now)
                expired = True
            else:
                updates = {}
                if to_state == "QUOTED":
                    if expires_at is not None and expires_at <= now:
                        raise OrderError("Cannot record an already expired quote")
                    quote_json = _json(evidence)
                    updates.update(quote_json=quote_json, quote_hash=_digest(quote_json),
                                   expires_at=expires_at, last_valid_block_height=last_valid_block_height)
                elif to_state == "SIMULATED":
                    if evidence.get("success") is not True:
                        raise OrderError("Successful simulation evidence is required")
                elif to_state == "PREPARED":
                    if row["mode"] != "LIVE":
                        raise OrderError("Paper orders cannot prepare or broadcast a signed transaction")
                    transaction_id = _text(evidence.get("transaction_id"), "transaction_id")
                    signed_hash = evidence.get("signed_message_hash")
                    if not isinstance(signed_hash, str) or len(signed_hash) != 64 or any(c not in "0123456789abcdef" for c in signed_hash):
                        raise OrderError("signed_message_hash must be a lowercase SHA-256 hex digest")
                    updates.update(transaction_id=transaction_id, signed_message_hash=signed_hash)
                elif to_state in {"SUBMITTING", "SUBMITTED", "UNCERTAIN"}:
                    if row["mode"] != "LIVE" or not row["transaction_id"] or not row["signed_message_hash"]:
                        raise OrderError("Submission requires a committed live signed-transaction identity")
                    if to_state == "SUBMITTED" and evidence.get("transaction_id") != row["transaction_id"]:
                        raise OrderError("Transport signature differs from prepared transaction identity")
                elif to_state == "CONFIRMED":
                    updates.update(self._confirmation(row, evidence))
                elif to_state in {"FAILED", "CANCELLED", "EXPIRED"}:
                    _text(evidence.get("reason"), "reason", 1024)
                    if to_state == "EXPIRED" and not self._expired(row, now, current_block_height):
                        raise OrderError("Expiry requires a known elapsed time or block-height limit")
                    if row["state"] in RECONCILIATION_STATES:
                        self._validate_failed_outcome(row, evidence)
                        updates.update(confirmation_json=_json(evidence), confirmed_simulated=0)
                    elif apply_failure is not None:
                        raise OrderError("Pre-submission failure cannot charge a chain execution fee")
                row = self._change(conn, row, to_state, evidence, now, updates)
                if to_state == "CONFIRMED" and apply_confirmation is not None:
                    self._apply_posting(conn, row, apply_confirmation, now)
                elif to_state == "FAILED" and apply_failure is not None:
                    self._apply_posting(conn, row, apply_failure, now, allow_failure=True)
            result = self._record(row)
            conn.commit()
        if expired:
            raise OrderExpired(result)
        return result

    @staticmethod
    def _posting(row):
        if row is None:
            return None
        return {"order_id": row["order_id"], "timestamp": row["timestamp"],
                "result": json.loads(row["result_json"]), "result_hash": row["result_hash"]}

    @staticmethod
    def _validate_failed_outcome(row, evidence):
        if (row["mode"] != "LIVE" or evidence.get("execution") != "LIVE" or
                evidence.get("transaction_id") != row["transaction_id"] or
                evidence.get("signed_message_hash") != row["signed_message_hash"] or
                evidence.get("reconciled") is not True or evidence.get("success") is not False or
                evidence.get("simulated") is not False or evidence.get("actual_money_used") is not True or
                evidence.get("confirmation_status") not in {"confirmed", "finalized"} or
                evidence.get("onchain_error") in (None, "", False)):
            raise OrderError("Ambiguous submission needs matching, reconciled chain failure evidence")
        _integer(evidence.get("fee_paid_raw"), "failed transaction fee")
        for field in ("input_spent_raw", "output_received_raw"):
            _integer(evidence.get(field), field, 0, 0)

    def _apply_posting(self, conn, row, apply, now, *, allow_failure=False):
        if allow_failure:
            if row["state"] != "FAILED" or row["confirmation_json"] is None:
                raise OrderError("Failed fee posting requires a reconciled on-chain failure")
            self._validate_failed_outcome(row, json.loads(row["confirmation_json"]))
        elif row["state"] != "CONFIRMED":
            raise OrderError("Only confirmed outcomes can affect the portfolio ledger")
        existing = conn.execute("SELECT * FROM execution_order_postings WHERE order_id=?", (row["id"],)).fetchone()
        if existing is not None:
            return self._posting(existing)
        # The supplied portfolio function must use this exact connection.
        # No network requests or additional connection/commit is permitted.
        result = apply(conn, self._record(row))
        if not isinstance(result, dict) or not result:
            raise OrderError("Portfolio applier must return a nonempty posting receipt")
        encoded = _json(result)
        conn.execute("INSERT INTO execution_order_postings(order_id,timestamp,result_json,result_hash) VALUES(?,?,?,?)",
                     (row["id"], now, encoded, _digest(encoded)))
        return self._posting(conn.execute("SELECT * FROM execution_order_postings WHERE order_id=?", (row["id"],)).fetchone())

    def get_posting(self, order_id):
        with self._connection() as conn:
            self._load(conn, order_id)
            return self._posting(conn.execute("SELECT * FROM execution_order_postings WHERE order_id=?", (order_id,)).fetchone())

    def pending_postings(self):
        """Confirmed execution evidence which still needs portfolio reconciliation."""
        with self._connection() as conn:
            return [self._record(row) for row in conn.execute("""SELECT * FROM execution_orders
                WHERE state='CONFIRMED' AND NOT EXISTS
                (SELECT 1 FROM execution_order_postings WHERE order_id=execution_orders.id)
                ORDER BY created_at,id""")]

    def apply_confirmed(self, order_id, apply):
        """Recover a confirmed/unposted outcome, atomically and exactly once.

        The callback receives the already locked SQLite connection. It must
        write all cash/token/fee/history changes on that same connection and
        perform no external action. A callback failure rolls every write back.
        """
        if not callable(apply):
            raise OrderError("A portfolio applier is required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load(conn, order_id)
            result = self._apply_posting(conn, row, apply, self._now())
            conn.commit()
            return result

    def pending_failed_postings(self):
        """Confirmed chain failures whose network fees have not been booked."""
        with self._connection() as conn:
            return [self._record(row) for row in conn.execute("""SELECT * FROM execution_orders
                WHERE state='FAILED' AND confirmation_json IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM execution_order_postings WHERE order_id=execution_orders.id)
                ORDER BY created_at,id""")]

    def apply_failed(self, order_id, apply):
        """Recover a reconciled failed-transaction fee exactly once, atomically."""
        if not callable(apply):
            raise OrderError("A failed-fee portfolio applier is required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load(conn, order_id)
            result = self._apply_posting(conn, row, apply, self._now(), allow_failure=True)
            conn.commit()
            return result

    def quote(self, order_id, quote_evidence, *, expected_version, expires_at=None, last_valid_block_height=None):
        return self.transition(order_id, "QUOTED", expected_version=expected_version, evidence=quote_evidence,
                               expires_at=expires_at, last_valid_block_height=last_valid_block_height)

    def prepare(self, order_id, *, transaction_id, signed_message_hash, expected_version,
                current_block_height=None, reconciliation=None):
        evidence = {"transaction_id": transaction_id, "signed_message_hash": signed_message_hash}
        if reconciliation is not None:
            if not isinstance(reconciliation, dict) or not reconciliation:
                raise OrderError("Reconciliation constraints must be a nonempty object")
            evidence["reconciliation"] = reconciliation
        return self.transition(order_id, "PREPARED", expected_version=expected_version,
                               evidence=evidence,
                               current_block_height=current_block_height)

    def events(self, order_id):
        with self._connection() as conn:
            self._load(conn, order_id)
            result = []
            for row in conn.execute("SELECT * FROM execution_order_events WHERE order_id=? ORDER BY version", (order_id,)):
                event = dict(row)
                event["evidence"] = json.loads(event.pop("evidence_json"))
                result.append(event)
            return result

    def pending_for_reconciliation(self):
        with self._connection() as conn:
            return [self._record(row) for row in conn.execute(
                "SELECT * FROM execution_orders WHERE state IN ('SUBMITTING','SUBMITTED','UNCERTAIN') ORDER BY created_at,id")]

    def recover_inflight(self):
        """On executor startup, turn interrupted broadcast attempts into UNCERTAIN."""
        recovered = []
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM execution_orders WHERE state='SUBMITTING' ORDER BY created_at,id").fetchall()
            for row in rows:
                new_row = self._change(conn, row, "UNCERTAIN", {
                    "reason": "Executor restarted while broadcast outcome was unknown",
                    "transaction_id": row["transaction_id"], "source": "recovery"}, self._now())
                recovered.append(self._record(new_row))
            conn.commit()
        return recovered
