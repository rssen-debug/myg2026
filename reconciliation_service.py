"""Persist prepared constraints and reconcile interrupted orders via reads only.

This connects chain evidence to OrderStore, not a LIVE trading adapter. It
cannot sign or submit. Chain-confirmed fills/failed fees stay explicitly
unposted unless a separate portfolio callback can account for them atomically.
"""
from dataclasses import asdict
import hashlib
import json

from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from order_lifecycle import OrderError, ConcurrentOrderUpdate, TERMINAL_STATES, _json
from transaction_reconciliation import ExpectedSwap, fetch_reconciliation


class ReconciliationService:
    def __init__(self, store, rpc, *, expected_genesis_hash, apply_confirmation=None, apply_failure=None):
        if not isinstance(expected_genesis_hash, str) or not expected_genesis_hash:
            raise ValueError("An independently configured chain identity is required")
        self.store, self.rpc, self.genesis = store, rpc, expected_genesis_hash
        self.apply_confirmation, self.apply_failure = apply_confirmation, apply_failure
        with store._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS execution_reconciliation_checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL REFERENCES execution_orders(id),
                    timestamp REAL NOT NULL, result_json TEXT NOT NULL, result_hash TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS reconciliation_checks_no_update
                    BEFORE UPDATE ON execution_reconciliation_checks BEGIN
                    SELECT RAISE(ABORT,'Reconciliation checks are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS reconciliation_checks_no_delete
                    BEFORE DELETE ON execution_reconciliation_checks BEGIN
                    SELECT RAISE(ABORT,'Reconciliation checks are immutable'); END;
            """)

    def prepare(self, order_id, expected, signed_transaction, *, expected_version, current_block_height):
        """Persist constraints BEFORE any future sender may claim SUBMITTING.

        The signer/binary-policy validator must be supplied by a future live
        adapter. This check binds identity; it does not approve signing policy.
        Signed bytes are verified locally and are not sent or logged here.
        """
        if not isinstance(expected, ExpectedSwap) or not isinstance(signed_transaction, bytes):
            raise OrderError("Validated expected constraints and exact signed bytes are required")
        try:
            tx = VersionedTransaction.from_bytes(signed_transaction)
            tx.sanitize()
            if (not 0 < len(signed_transaction) <= 1232 or bytes(tx) != signed_transaction or
                    not tx.signatures or not all(tx.verify_with_results()) or
                    str(tx.signatures[0]) != expected.signature or
                    hashlib.sha256(to_bytes_versioned(tx.message)).hexdigest() != expected.message_sha256 or
                    tx.message.header.num_required_signatures != 1 or str(tx.message.account_keys[0]) != expected.wallet):
                raise ValueError("Identity mismatch")
        except Exception as exc:
            raise OrderError("Prepared transaction identity cannot be verified") from exc
        order = self.store.get(order_id)
        intent = order["intent"]
        if (intent["mode"] != "LIVE" or intent["input_mint"] != expected.input_mint or
                intent["output_mint"] != expected.output_mint or intent["amount_raw"] != expected.input_raw):
            raise OrderError("Prepared constraints differ from immutable order intent")
        quote = (order.get("quote") or {}).get("quote", {})
        minimum = quote.get("min_out_amount_raw")
        if type(minimum) is not int or minimum <= 0 or expected.minimum_output_raw < minimum:
            raise OrderError("Prepared constraints weaken or lack the stored quote minimum output")
        return self.store.prepare(order_id, transaction_id=expected.signature, signed_message_hash=expected.message_sha256,
            expected_version=expected_version, current_block_height=current_block_height,
            reconciliation={"expected": asdict(expected), "genesis_hash": self.genesis})

    def _expected(self, order):
        events = [event for event in self.store.events(order["id"]) if event["to_state"] == "PREPARED"]
        if len(events) != 1:
            raise OrderError("Exactly one persisted prepared identity is required")
        saved = events[0]["evidence"].get("reconciliation")
        if not isinstance(saved, dict) or saved.get("genesis_hash") != self.genesis:
            raise OrderError("Persisted reconciliation constraints/chain identity missing or changed")
        try:
            expected = ExpectedSwap(**saved["expected"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderError("Invalid persisted reconciliation constraints") from exc
        if expected.signature != order["transaction_id"] or expected.message_sha256 != order["signed_message_hash"]:
            raise OrderError("Persisted identity differs from prepared constraints")
        return expected

    def reconcile(self, order_id):
        order = self.store.get(order_id)
        if order["state"] in TERMINAL_STATES:
            return order
        if order["mode"] != "LIVE" or order["state"] not in {"SUBMITTING", "SUBMITTED", "UNCERTAIN"}:
            raise OrderError("Only potentially submitted live orders can be reconciled")
        expected = self._expected(order)
        if order["state"] == "SUBMITTING":
            try:
                order = self.store.transition(order_id, "UNCERTAIN", expected_version=order["version"],
                    evidence={"reason": "Recovering interrupted submission through chain reads only",
                              "transaction_id": expected.signature})
            except ConcurrentOrderUpdate:
                return self.store.get(order_id)
        outcome = fetch_reconciliation(self.rpc, expected, expected_genesis_hash=self.genesis)
        encoded = _json(outcome)
        with self.store._connection() as conn:
            conn.execute("INSERT INTO execution_reconciliation_checks(order_id,timestamp,result_json,result_hash) VALUES(?,?,?,?)",
                (order_id, self.store._now(), encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        if outcome["state"] == "UNCERTAIN" and order["state"] == "UNCERTAIN":
            return self.store.get(order_id)
        try:
            return self.store.transition(order_id, outcome["state"], expected_version=order["version"],
                evidence=outcome["evidence"],
                apply_confirmation=self.apply_confirmation if outcome["state"] == "CONFIRMED" else None,
                apply_failure=self.apply_failure if outcome["state"] == "FAILED" else None)
        except ConcurrentOrderUpdate:
            return self.store.get(order_id)

    def recover_all(self):
        """Bounded by stored pending orders; never resubmit interrupted bytes."""
        return [self.reconcile(order["id"]) for order in self.store.pending_for_reconciliation()]

    def checks(self, order_id):
        with self.store._connection() as conn:
            return [{"timestamp": row["timestamp"], "outcome": json.loads(row["result_json"])} for row in conn.execute(
                "SELECT timestamp,result_json FROM execution_reconciliation_checks WHERE order_id=? ORDER BY id", (order_id,))]
