"""并发发布：乐观锁保证同一记录只有一个发布事件。"""
from __future__ import annotations

from _base import BackendTestCase, YEAR

from assessment_backend import ConcurrencyError, WorkflowError
from assessment_backend.projection import Projection



class ConcurrentPublishTest(BackendTestCase):
    def _ready_record(self) -> None:
        self.seal_skill_caliber()
        self.svc.open_record(self.manager, "R1", "U1", YEAR)
        self.svc.record_value(self.filer, "R1", "SK1", 88.0, "E1")
        self.svc.record_value(self.filer, "R1", "SV1", 90.0, "E2")
        self.svc.record_value(self.filer, "R1", "RS1", 70.0, "E3")

    def test_only_one_publish_wins(self) -> None:
        self._ready_record()
        base = self.seq()
        self.svc.publish_record(self.manager, "R1", expected_seq=base)
        with self.assertRaises(ConcurrencyError):
            self.svc.publish_record(self.manager_b, "R1", expected_seq=base)

        publish_events = [e for e in self.store.all_events()
                          if e.type == "RecordPublished" and e.payload["record_id"] == "R1"]
        self.assertEqual(len(publish_events), 1)
        self.assertEqual(publish_events[0].actor_id, "赵管理")

        race = self.audit.inspect_publish_race("R1")
        self.assertEqual(race["winning_actor"], "赵管理")
        self.assertEqual(race["winning_seq"], publish_events[0].seq)

    def test_second_publish_after_win_is_workflow_error(self) -> None:
        self._ready_record()
        self.svc.publish_record(self.manager, "R1")
        # 落败方重读后再试：记录已公布，只能更正
        with self.assertRaises(WorkflowError):
            self.svc.publish_record(self.manager_b, "R1")

    def test_unrelated_concurrent_appends_are_serialized(self) -> None:
        # expected_seq 不匹配即拒绝，调用方必须重读，状态不被部分写入
        self._ready_record()
        base = self.seq()
        with self.assertRaises(ConcurrencyError):
            self.svc.publish_record(self.manager, "R1", expected_seq=base + 5)
        self.assertFalse(Projection.rebuild(self.store.all_events()).records["R1"].published)
