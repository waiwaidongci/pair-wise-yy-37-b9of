import json
import tempfile
import threading
import unittest
import urllib.request
from http.client import RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.domain import ConflictError
from src.http_api import make_handler
from src.ledger_service import LedgerService
from src.ledger_store import LedgerStore
from src.repository import Repository
from src.service import Service

T08 = "2026-10-06T08:00Z"
T09 = "2026-10-06T09:00Z"
T10 = "2026-10-06T10:00Z"
RANGE_MORNING = {"from": T08, "to": T10}


class CapacityFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "cap.db"))
        self.store = LedgerStore(self.repo.conn, self.repo._lock)
        self.service = Service(self.repo)
        self.ledger = LedgerService(self.repo, self.store)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _facility(self, code="F1", capacity=100.0):
        return self.ledger.create_facility(
            {"code": code, "name": "一号设施", "capacity": capacity},
            "clerk", "applicant")

    def _basis(self, facility_id, kind, amount, start=T08, end=T09,
               actor="wang", role="inspector", **extra):
        payload = {"source_kind": kind, "conclusion": f"{kind}结论",
                   "reduce_amount": amount, "effective_from": start,
                   "effective_to": end}
        payload.update(extra)
        return self.ledger.register_basis(facility_id, payload, actor, role)

    def test_four_bases_share_one_capacity_slice(self):
        f = self._facility()
        self._basis(f["id"], "maintenance", 30)
        self._basis(f["id"], "inspection", 50, actor="zhao")
        self._basis(f["id"], "permit", 40, actor="factory", role="applicant")
        self._basis(f["id"], "audit", 20, actor="auditor",
                    role="compliance_manager")
        view = self.ledger.ledger("viewer", f["id"], T08, T08)
        row = view["slots"][0]
        self.assertEqual(row["merged_basis"]["reduce_amount"], 50)  # 取最大不叠加
        self.assertEqual(row["basis_count"], 4)
        # 一次调度占用同一份容量
        result = self.ledger.dispatch({
            "client_token": "B-1",
            "lines": [{"facility_id": f["id"], "slot_start": T08}]},
            "wang", "inspector")
        self.assertEqual(result["lines"][0]["status"], "issued")
        self.assertEqual(result["lines"][0]["reduce_amount"], 50)
        view = self.ledger.ledger("viewer", f["id"], T08, T08)
        self.assertEqual(view["slots"][0]["occupied_amount"], 50)
        self.assertEqual(view["slots"][0]["remaining"], 50)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_basis_update_recomputes_unexecuted_and_holds_issued(self):
        f = self._facility()
        b1 = self._basis(f["id"], "maintenance", 30)
        self.ledger.refresh_plan(
            {"facility_id": f["id"], **RANGE_MORNING}, "wang", "inspector")
        planned = self.store.list_directives(f["id"], "planned")
        self.assertEqual({d["slot_start"] for d in planned}, {T08, T09})
        # 08点下达（占30）
        res = self.ledger.dispatch({
            "client_token": "B-2",
            "lines": [{"facility_id": f["id"], "slot_start": T08,
                       "external_ref": "D-08"}]}, "wang", "inspector")
        self.assertEqual(res["lines"][0]["status"], "issued")
        # 检修结论从30降到20
        self.ledger.update_basis(b1["id"], {
            "conclusion": "检修结论更新", "reduce_amount": 20,
            "effective_from": T08, "effective_to": T09,
            "expected_version": 1}, "wang", "inspector")
        held = self.store.live_directive(f["id"], T08)
        self.assertEqual(held["status"], "issued")       # 已下达保留
        self.assertEqual(held["reduce_amount"], 30)      # 容量不被抢占
        self.assertTrue(held["pending_basis"])           # 待补依据
        unexecuted = self.store.live_directive(f["id"], T09)
        self.assertEqual(unexecuted["status"], "planned")
        self.assertEqual(unexecuted["reduce_amount"], 20)  # 未执行按新依据重算
        # 补足依据后待补标记清除
        self._basis(f["id"], "inspection", 40, start=T08, end=T08,
                    actor="zhao")
        held = self.store.live_directive(f["id"], T08)
        self.assertFalse(held["pending_basis"])
        # 检查结论不足时补依据被拒
        self._basis(f["id"], "permit", 30, start=T10, end=T10,
                    actor="factory", role="applicant")
        issued10 = self.ledger.dispatch({
            "client_token": "B-2b",
            "lines": [{"facility_id": f["id"], "slot_start": T10,
                       "reduce_amount": 60, "external_ref": "D-10"}]},
            "wang", "inspector")
        self.assertTrue(issued10["lines"][0]["pending_basis"])
        weak_basis = self.store.list_active_bases(f["id"], T10, T10)[0]
        with self.assertRaises(ConflictError):
            self.ledger.supplement_basis(
                issued10["lines"][0]["directive_id"],
                {"basis_id": weak_basis["id"], "note": "依据不足"},
                "auditor", "compliance_manager")

    def test_two_regulators_first_comes_first_served(self):
        f = self._facility(capacity=100.0)
        self._basis(f["id"], "inspection", 60)
        first = self.ledger.dispatch({
            "client_token": "R-A",
            "lines": [{"facility_id": f["id"], "slot_start": T08,
                       "external_ref": "RA-08"}]}, "regulator_a", "inspector")
        self.assertEqual(first["lines"][0]["status"], "issued")
        second = self.ledger.dispatch({
            "client_token": "R-B",
            "lines": [{"facility_id": f["id"], "slot_start": T08,
                       "reduce_amount": 60, "external_ref": "RB-08"}]},
            "regulator_b", "inspector")
        self.assertEqual(second["lines"][0]["status"], "conflicted")
        self.assertEqual(second["lines"][0]["remaining"], 40)  # 看到剩余容量
        self.assertEqual(second["lines"][0]["holder_directive_id"],
                         first["lines"][0]["directive_id"])
        # 占用只有一份，没有被叠加
        row = self.ledger.ledger("viewer", f["id"], T08, T08)["slots"][0]
        self.assertEqual(row["occupied_amount"], 60)

    def test_batch_write_failure_recovery_is_idempotent(self):
        f = self._facility()
        self._basis(f["id"], "maintenance", 20)
        self._basis(f["id"], "inspection", 30, start=T09, end=T09, actor="zhao")
        # 第一行成功，第二行写入失败
        self.ledger._fail_allocations = {2}
        with self.assertRaises(ConflictError):
            self.ledger.dispatch({
                "client_token": "BATCH-X",
                "lines": [
                    {"facility_id": f["id"], "slot_start": T08,
                     "external_ref": "X-1"},
                    {"facility_id": f["id"], "slot_start": T09,
                     "external_ref": "X-2"}]}, "wang", "inspector")
        # 第一行已落库占容量
        self.assertEqual(
            self.ledger.ledger("viewer", f["id"], T08, T08)["slots"][0][
                "occupied_amount"], 20)
        # 用同批次恢复：重提第一行不重复占容量，第二行补占
        recovered = self.ledger.recover_batch(
            self.store.find_batch_by_token("BATCH-X")["id"], {
                "lines": [
                    {"facility_id": f["id"], "slot_start": T08,
                     "external_ref": "X-1"},
                    {"facility_id": f["id"], "slot_start": T09,
                     "external_ref": "X-2"}]}, "wang", "inspector")
        by_ref = {d["external_ref"]: d
                  for d in self.store.list_batch_directives(recovered["batch_id"])}
        self.assertEqual(by_ref["X-1"]["status"], "issued")
        self.assertEqual(by_ref["X-2"]["status"], "issued")
        self.assertTrue(any(l.get("retried") for l in recovered["lines"]))
        self.assertEqual(
            self.ledger.ledger("viewer", f["id"], T08, T08)["slots"][0][
                "occupied_amount"], 20)
        self.assertEqual(recovered["batch_status"], "completed")

    def test_legacy_without_basis_becomes_pending_baseline(self):
        f = self._facility()
        # 旧库记录：没有任何容量依据
        result = self.ledger.legacy_import({
            "lines": [{"facility_code": "F1", "slot_start": T08,
                       "reduce_amount": 40, "external_ref": "OLD-1"}]},
            "auditor", "compliance_manager")
        line = result["lines"][0]
        self.assertEqual(line["status"], "baseline")
        self.assertFalse(line["verified"])
        self.assertTrue(line["pending_basis"])
        self.assertEqual(result["pending_verification"], [line["directive_id"]])
        # 基线同样占容量，新调度只看到剩余
        blocked = self.ledger.dispatch({
            "client_token": "AFTER-OLD",
            "lines": [{"facility_id": f["id"], "slot_start": T08,
                       "reduce_amount": 70, "external_ref": "NEW-1"}]},
            "wang", "inspector")
        self.assertEqual(blocked["lines"][0]["status"], "conflicted")
        self.assertEqual(blocked["lines"][0]["remaining"], 60)
        # 依据补齐并覆盖40后，基线自动核验
        self._basis(f["id"], "permit", 40, actor="factory",
                    role="applicant")
        held = self.store.live_directive(f["id"], T08)
        self.assertEqual(held["status"], "baseline")
        self.assertTrue(held["verified"])
        self.assertFalse(held["pending_basis"])
        # 旧库重提幂等
        again = self.ledger.legacy_import({
            "lines": [{"facility_code": "F1", "slot_start": T08,
                       "reduce_amount": 40, "external_ref": "OLD-1"}]},
            "auditor", "compliance_manager")
        self.assertTrue(again["lines"][0].get("retried"))
        self.assertEqual(
            self.ledger.ledger("viewer", f["id"], T08, T08)["slots"][0][
                "occupied_amount"], 40)

    def test_supersede_and_revoke_ranges(self):
        f = self._facility()
        b1 = self._basis(f["id"], "maintenance", 30)
        b2 = self._basis(f["id"], "maintenance", 25,
                         supersedes_id=b1["id"], actor="wang")
        self.assertEqual(self.store.get_basis(b1["id"])["state"], "superseded")
        merged = self.ledger.ledger("viewer", f["id"], T08, T08)["slots"][0]
        self.assertEqual(merged["merged_basis"]["reduce_amount"], 25)
        directives = self.store.list_directives(f["id"], "planned")
        self.assertTrue(directives)
        self.store.set_basis_state(b2["id"], "revoked")
        self.ledger.refresh_plan(
            {"facility_id": f["id"], **RANGE_MORNING}, "wang", "inspector")
        live = [d for d in self.store.list_directives(f["id"])
                if d["status"] != "cancelled"]
        self.assertEqual(live, [])


class CapacityHttpConcurrencyTest(unittest.TestCase):
    """HTTP层面验证两名监管员并发抢占：服务串行化、先到者占用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        self.store = LedgerStore(self.repo.conn, self.repo._lock)
        service = Service(self.repo)
        ledger = LedgerService(self.repo, self.store)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(service, ledger, static_dir))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.ledger = ledger

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.repo.close()
        self.tmp.cleanup()

    def _post(self, path, payload, actor, role):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Actor": actor, "X-Role": role}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_concurrent_dispatch_race(self):
        self._post("/api/facilities",
                   {"code": "F9", "name": "并发设施", "capacity": 100},
                   "clerk", "applicant")
        self._post("/api/facilities/1/bases",
                   {"source_kind": "inspection", "conclusion": "检查限产60",
                    "reduce_amount": 60, "effective_from": T08,
                    "effective_to": T08}, "wang", "inspector")
        barrier = threading.Barrier(2)
        results = {}

        def fire(name, token):
            barrier.wait()
            results[name] = self._post("/api/dispatch", {
                "client_token": token,
                "lines": [{"facility_id": 1, "slot_start": T08,
                           "external_ref": token}]}, name, "inspector")

        t1 = threading.Thread(target=fire, args=("alpha", "T-A"))
        t2 = threading.Thread(target=fire, args=("beta", "T-B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = [results["alpha"][1]["lines"][0]["status"],
                    results["beta"][1]["lines"][0]["status"]]
        self.assertEqual(sorted(statuses), ["conflicted", "issued"])
        winner = results["alpha"] if statuses[0] == "issued" else results["beta"]
        loser = results["beta"] if statuses[0] == "issued" else results["alpha"]
        self.assertEqual(loser[1]["lines"][0]["remaining"], 40)
        self.assertEqual(loser[1]["lines"][0]["holder_directive_id"],
                         winner[1]["lines"][0]["directive_id"])


if __name__ == "__main__":
    unittest.main()
