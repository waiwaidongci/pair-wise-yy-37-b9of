import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import CapacityConflictError, DispatchWriteError, PermissionDenied
from src.repository import Repository
from src.service import Service

MGR = "compliance_manager"
INSP = "inspector"
VIEW = "viewer"
SLOT_A = "2026-10-06T08"
SLOT_B = "2026-10-06T09"


class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _facility(self, code="F1", baseline=0.0):
        return self.service.create_facility(
            {"code": code, "name": "设施一", "baseline_capacity": baseline},
            "creator", MGR)

    def _basis(self, facility_id, kind="maintenance", capacity=100.0,
               conclusion="设备正常", ref=None):
        return self.service.record_basis(
            facility_id,
            {"kind": kind, "conclusion": conclusion, "capacity": capacity, "ref": ref},
            "recorder", INSP)

    # 1. 多设施负荷压减 + 四类依据各记一套 + 同一张分时容量台
    def test_facilities_and_four_basis_kinds_and_board(self):
        f1 = self._facility("F1")
        f2 = self._facility("F2", baseline=10.0)
        self.assertEqual(len(self.service.list_facilities(VIEW)), 2)
        for kind in ("maintenance", "inspection", "permit", "audit"):
            self._basis(f1["id"], kind=kind, capacity=100.0, conclusion=kind + "结论")
        basis = self.service.list_basis(f1["id"], VIEW)
        self.assertEqual(len(basis), 4)
        self.assertEqual({b["kind"] for b in basis},
                         {"maintenance", "inspection", "permit", "audit"})
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["total_capacity"], 100.0)
        self.assertEqual(board["basis_status"], "ok")
        self.assertEqual(board["occupied_capacity"], 0.0)
        self.assertEqual(board["remaining"], 100.0)
        # 不同设施同一时段各自独立成台
        board2 = self.service.get_capacity_board(f2["id"], SLOT_A, VIEW)
        self.assertEqual(board2["basis_status"], "pending_verification")

    # 2. 同一设施同一时段只占一份减排容量（容量台唯一行）
    def test_board_unique_per_facility_slot(self):
        f1 = self._facility()
        self._basis(f1["id"], capacity=100.0)
        self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        boards = self.service.list_boards(VIEW, f1["id"])
        self.assertEqual(len(boards), 1)
        self.service.get_capacity_board(f1["id"], SLOT_B, VIEW)
        self.assertEqual(len(self.service.list_boards(VIEW, f1["id"])), 2)

    # 3. 抢占容量：先到者占用，后到者看到剩余容量
    def test_seize_capacity_first_come_first_served(self):
        f1 = self._facility()
        self._basis(f1["id"], capacity=100.0)
        board = self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 60}, "sup", INSP)
        self.assertEqual(board["occupied_capacity"], 60.0)
        self.assertEqual(board["remaining"], 40.0)
        with self.assertRaises(CapacityConflictError) as ctx:
            self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 60}, "sup2", INSP)
        self.assertEqual(ctx.exception.remaining, 40.0)
        self.assertEqual(ctx.exception.total, 100.0)
        self.assertEqual(ctx.exception.slot, SLOT_A)
        # 后到者只看到剩余容量，未占用
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["occupied_capacity"], 60.0)
        self.assertEqual(board["remaining"], 40.0)
        # 恰好等于剩余容量可全部占用
        board = self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 40}, "sup3", INSP)
        self.assertEqual(board["remaining"], 0.0)

    # 4. 两名监管员同时抢占同一时段：先到者占用，后到者看到剩余容量
    def test_concurrent_seize_race(self):
        f1 = self._facility()
        self._basis(f1["id"], capacity=100.0)
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                results.append(self.service.seize_capacity(
                    f1["id"], {"slot": SLOT_A, "amount": 60}, "sup", INSP))
            except CapacityConflictError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(results), 1, "恰有一人抢占成功")
        self.assertEqual(len(errors), 1, "恰有一人看到剩余容量")
        self.assertEqual(results[0]["occupied_capacity"], 60.0)
        self.assertEqual(errors[0].remaining, 40.0)
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["occupied_capacity"], 60.0)
        self.assertEqual(board["remaining"], 40.0)

    # 5. 检修/检查结论更新后：未执行指令按新依据重算，已下达指令保留待补依据
    def test_basis_conclusion_update_recalc(self):
        f1 = self._facility()
        basis = self._basis(f1["id"], kind="maintenance", capacity=100.0)
        # 未执行（planned）指令：抢占产生
        self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 60}, "sup", INSP)
        # 已下达（issued）指令：调度批次产生
        self.service.submit_batch(
            {"batch_key": "B-1", "facility_id": f1["id"], "slot": SLOT_B, "amount": 50},
            "disp", INSP)
        # 更新检修结论，新依据容量降为 80
        updated = self.service.update_basis_conclusion(
            basis["id"], {"conclusion": "检修后复核", "capacity": 80}, "recorder", INSP)
        self.assertEqual(updated["capacity"], 80.0)
        instructions = self.service.list_instructions(f1["id"], VIEW)
        planned = [i for i in instructions if i["status"] == "planned"]
        issued = [i for i in instructions if i["status"] == "issued"]
        self.assertEqual(len(planned), 1)
        self.assertEqual(len(issued), 1)
        # 未执行指令按新依据重算
        self.assertEqual(planned[0]["amount"], 80.0)
        # 已下达指令保留，待补依据
        self.assertEqual(issued[0]["amount"], 50.0)
        self.assertEqual(issued[0]["basis_status"], "pending_supplement")
        board_a = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board_a["total_capacity"], 80.0)
        self.assertEqual(board_a["occupied_capacity"], 80.0)
        self.assertEqual(board_a["basis_status"], "ok")
        board_b = self.service.get_capacity_board(f1["id"], SLOT_B, VIEW)
        self.assertEqual(board_b["total_capacity"], 80.0)
        self.assertEqual(board_b["occupied_capacity"], 50.0)

    # 6. 非检修/检查依据（许可/审计）结论更新不触发指令重算
    def test_permit_audit_update_no_recalc(self):
        f1 = self._facility()
        permit = self._basis(f1["id"], kind="permit", capacity=100.0)
        self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 60}, "sup", INSP)
        self.service.update_basis_conclusion(
            permit["id"], {"conclusion": "许可续期", "capacity": 120}, "recorder", MGR)
        instructions = self.service.list_instructions(f1["id"], VIEW)
        self.assertEqual(instructions[0]["amount"], 60.0)
        self.assertEqual(instructions[0]["basis_status"], "ok")

    # 7. 调度写入失败后按批次恢复，重提不重复占容量
    def test_batch_recovery_idempotent(self):
        f1 = self._facility()
        self._basis(f1["id"], capacity=100.0)
        # 模拟写入失败：批次头已落库为 pending，写入步骤回滚
        with self.assertRaises(DispatchWriteError):
            self.service.submit_batch(
                {"batch_key": "B-1", "facility_id": f1["id"], "slot": SLOT_A,
                 "amount": 60, "simulate_failure": True}, "disp", INSP)
        batch = self.service.get_batch("B-1", VIEW)
        self.assertEqual(batch["status"], "pending")
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["occupied_capacity"], 0.0, "失败回滚，未占容量")
        # 按批次恢复
        recovered = self.service.recover_batch("B-1", "disp", INSP)
        self.assertEqual(recovered["status"], "committed")
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["occupied_capacity"], 60.0)
        # 重提不重复占容量
        again = self.service.submit_batch(
            {"batch_key": "B-1", "facility_id": f1["id"], "slot": SLOT_A, "amount": 60},
            "disp", INSP)
        self.assertEqual(again["status"], "committed")
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["occupied_capacity"], 60.0)

    # 8. 旧库缺容量依据时升级为待核验基线
    def test_legacy_missing_basis_pending_verification(self):
        f1 = self._facility(baseline=25.0)
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["basis_status"], "pending_verification")
        self.assertEqual(board["total_capacity"], 25.0)
        self.assertEqual(board["remaining"], 25.0)
        # 抢占仍可按基线容量进行
        seized = self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 10}, "sup", INSP)
        self.assertEqual(seized["basis_status"], "pending_verification")
        self.assertEqual(seized["remaining"], 15.0)
        # 补录依据后恢复正常
        self._basis(f1["id"], capacity=100.0)
        board = self.service.get_capacity_board(f1["id"], SLOT_A, VIEW)
        self.assertEqual(board["basis_status"], "ok")
        self.assertEqual(board["total_capacity"], 100.0)
        # 迁移函数对无依据设施幂等
        result = self.service.migrate_pending_verification("admin", MGR)
        self.assertGreaterEqual(result["updated_boards"], 0)

    # 权限：只有调度角色可抢占/提交批次
    def test_dispatch_requires_role(self):
        f1 = self._facility()
        self._basis(f1["id"], capacity=100.0)
        with self.assertRaises(PermissionDenied):
            self.service.seize_capacity(f1["id"], {"slot": SLOT_A, "amount": 10}, "attacker", VIEW)
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch(
                {"batch_key": "B-X", "facility_id": f1["id"], "slot": SLOT_A, "amount": 10},
                "attacker", VIEW)


if __name__ == "__main__":
    unittest.main()
