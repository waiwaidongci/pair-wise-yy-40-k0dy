import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import TRANSITION_ROLES
class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        item=self.service.create_item({"title":"plan item","description":"plan scenarios","severity":'high',"quantity":5,"threshold":10,"external_ref":"PLAN-1"},"creator",'assessor')
        self.record=self.service.add_record(item["id"],{"kind":"evidence","detail":"closed evaluation","status":"closed","external_ref":"PE-1"},"recorder",'assessor')
        self.item=self.service.transition(item["id"],'assessed',item["version"],"reviewer",TRANSITION_ROLES['assessed'][0])
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def submit(self,record_ids=None,content="reinforcement plan"):
        ids=record_ids if record_ids is not None else [self.record["id"]]
        return self.service.submit_plan(self.item["id"],{"content":content,"record_ids":ids},"engineer",'structural_engineer')
    def approve(self,plan_id):
        return self.service.review_plan(plan_id,{"decision":"approve"},"board",'review_board')
    def advance(self,target):
        self.item=self.service.transition(self.item["id"],target,self.item["version"],"reviewer",TRANSITION_ROLES[target][0])
        return self.item
    def test_approved_plan_allows_design_and_display(self):
        with self.assertRaises(ConflictError): self.advance('design')
        plan=self.submit()
        view=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(view["plan_version"],1); self.assertEqual(view["plan_status"],'pending'); self.assertEqual(view["design_blockers"],["加固方案待评审"])
        approved=self.approve(plan["id"])
        self.assertEqual(approved["status"],'approved'); self.assertEqual(approved["reviewed_by"],"board")
        view=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(view["plan_status"],'approved'); self.assertEqual(view["plan_reviewed_by"],"board"); self.assertEqual(view["design_blockers"],[])
        self.advance('design'); self.assertEqual(self.item["status"],'design')
        listed=self.service.list_items("viewer")[0]
        self.assertEqual(listed["plan_version"],1); self.assertEqual(listed["plan_reviewed_by"],"board")
    def test_single_pending_per_item(self):
        self.submit()
        with self.assertRaises(ConflictError): self.submit()
    def test_reject_requires_reason_and_blocks_design(self):
        plan=self.submit()
        with self.assertRaises(ValidationError): self.service.review_plan(plan["id"],{"decision":"reject"},"board",'review_board')
        rejected=self.service.review_plan(plan["id"],{"decision":"reject","reason":"依据不足"},"board",'review_board')
        self.assertEqual(rejected["status"],'rejected'); self.assertEqual(rejected["review_reason"],"依据不足")
        with self.assertRaises(ConflictError): self.advance('design')
        view=self.service.get_item(self.item["id"],"viewer")
        self.assertTrue(any("依据不足" in blocker for blocker in view["design_blockers"]))
        with self.assertRaises(ConflictError): self.approve(plan["id"])
    def test_new_version_invalidates_previous_approval(self):
        plan=self.submit(); self.approve(plan["id"])
        second=self.submit(content="revised plan")
        self.assertEqual(second["version_no"],2)
        old=self.service.get_plan(plan["id"],"viewer")
        self.assertEqual(old["status"],'invalidated'); self.assertEqual(old["invalidated_reason"],"方案换版")
        with self.assertRaises(ConflictError): self.advance('design')
        self.approve(second["id"]); self.advance('design')
    def test_record_amend_invalidates_pending_and_approved(self):
        plan=self.submit()
        result=self.service.amend_record(self.item["id"],self.record["id"],{"detail":"amended evaluation"},"recorder",'assessor')
        self.assertEqual(result["invalidated_plan_ids"],[plan["id"]])
        self.assertEqual(self.service.get_plan(plan["id"],"viewer")["invalidated_reason"],"评估事项补改")
        second=self.submit(); self.approve(second["id"])
        result=self.service.amend_record(self.item["id"],self.record["id"],{"detail":"amended again"},"recorder",'assessor')
        self.assertEqual(result["invalidated_plan_ids"],[second["id"]])
        view=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(view["plan_status"],'invalidated')
        self.assertTrue(any("评估事项补改" in blocker for blocker in view["design_blockers"]))
        with self.assertRaises(ConflictError): self.advance('design')
    def test_construction_does_not_regress(self):
        plan=self.submit(); self.approve(plan["id"]); self.advance('design'); self.advance('construction')
        result=self.service.amend_record(self.item["id"],self.record["id"],{"detail":"late amendment"},"recorder",'assessor')
        self.assertEqual(result["invalidated_plan_ids"],[plan["id"]])
        view=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(view["status"],'construction'); self.assertEqual(view["plan_status"],'invalidated'); self.assertEqual(view["design_blockers"],[])
        with self.assertRaises(ConflictError): self.submit()
        self.advance('accepted'); self.assertEqual(self.item["status"],'accepted')
    def test_stale_pending_cannot_be_approved(self):
        plan=self.submit()
        with self.repo.conn: self.repo.conn.execute("UPDATE records SET version=version+1 WHERE id=?",(self.record["id"],))
        self.assertTrue(self.service.get_plan(plan["id"],"viewer")["stale"])
        with self.assertRaises(ConflictError): self.approve(plan["id"])
    def test_basis_must_be_closed_known_records(self):
        open_record=self.service.add_record(self.item["id"],{"kind":"action","detail":"still open","status":"open","external_ref":"PE-2"},"recorder",'assessor')
        with self.assertRaises(ConflictError): self.submit([open_record["id"]])
        with self.assertRaises(ValidationError): self.submit([9999])
        with self.assertRaises(ValidationError): self.submit([])
        with self.assertRaises(ValidationError): self.submit(["1"])
    def test_role_guards(self):
        with self.assertRaises(PermissionDenied): self.service.submit_plan(self.item["id"],{"content":"x","record_ids":[self.record["id"]]},"creator",'assessor')
        plan=self.submit()
        with self.assertRaises(PermissionDenied): self.service.review_plan(plan["id"],{"decision":"approve"},"engineer",'structural_engineer')
        with self.assertRaises(PermissionDenied): self.service.amend_record(self.item["id"],self.record["id"],{"detail":"y"},"board",'review_board')
    def test_submit_state_guard(self):
        fresh=self.service.create_item({"title":"fresh","description":"not assessed","severity":'low',"quantity":1,"threshold":10,"external_ref":"PLAN-2"},"creator",'assessor')
        with self.assertRaises(ConflictError): self.service.submit_plan(fresh["id"],{"content":"x","record_ids":[self.record["id"]]},"engineer",'structural_engineer')
    def test_amend_validation(self):
        with self.assertRaises(ValidationError): self.service.amend_record(self.item["id"],self.record["id"],{},"recorder",'assessor')
        with self.assertRaises(ValidationError): self.service.amend_record(self.item["id"],self.record["id"],{"status":"bad"},"recorder",'assessor')
        with self.assertRaises(NotFoundError): self.service.amend_record(self.item["id"],9999,{"detail":"x"},"recorder",'assessor')
        result=self.service.amend_record(self.item["id"],self.record["id"],{"status":"open"},"recorder",'assessor')
        self.assertEqual(result["record"]["status"],'open'); self.assertEqual(result["record"]["version"],2)
    def test_plan_audit_trail(self):
        plan=self.submit(); self.approve(plan["id"])
        events=[e for e in self.repo.list_audit() if e["entity_type"]=='加固方案']
        self.assertEqual([e["action"] for e in events],["plan_submit","plan_review"])
        self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
