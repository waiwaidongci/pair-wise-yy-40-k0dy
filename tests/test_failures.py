import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'high',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'assessor')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _approve_plan(self,item):
        rec=self.service.add_record(item["id"],{"kind":"assessment","detail":"closed assessment","status":"closed","external_ref":"AS-F"},"recorder",'assessor')
        plan=self.service.submit_plan(item["id"],{"content":"p","basis_record_ids":[rec["id"]]},"eng",'structural_engineer')
        return self.service.review_plan(plan["id"],{"decision":"approved","expected_version":1},"board",'review_board')
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'assessor')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'assessor')
        current=self.service.get_item(self.item["id"],"viewer")
        current=self.service.transition(current["id"],STATES[1],current["version"],"reviewer",TRANSITION_ROLES[STATES[1]][0])
        # 复核未通过不能进设计
        with self.assertRaises(ConflictError): self.service.transition(current["id"],"design",current["version"],"eng",TRANSITION_ROLES["design"][0])
        self._approve_plan(current)
        current=self.service.transition(current["id"],"design",current["version"],"eng",TRANSITION_ROLES["design"][0])
        current=self.service.transition(current["id"],STATES[3],current["version"],"reviewer",TRANSITION_ROLES[STATES[3]][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],"accepted",current["version"],"reviewer",TRANSITION_ROLES["accepted"][0])
    def test_plan_requires_closed_assessment_items(self):
        assessed=self.service.transition(self.item["id"],"assessed",1,"r",'assessor')
        open_rec=self.service.add_record(assessed["id"],{"kind":"assessment","detail":"still open","status":"open"},"r",'assessor')
        with self.assertRaises(ValidationError):
            self.service.submit_plan(assessed["id"],{"content":"p","basis_record_ids":[]},"eng",'structural_engineer')
        with self.assertRaises(ConflictError):
            self.service.submit_plan(assessed["id"],{"content":"p","basis_record_ids":[open_rec["id"]]},"eng",'structural_engineer')
        with self.assertRaises(PermissionDenied):
            self.service.submit_plan(assessed["id"],{"content":"p","basis_record_ids":[999]},"eng",'viewer')
    def test_only_one_pending_and_reject_needs_reason(self):
        assessed=self.service.transition(self.item["id"],"assessed",1,"r",'assessor')
        rec=self.service.add_record(assessed["id"],{"kind":"assessment","detail":"closed","status":"closed"},"r",'assessor')
        plan=self.service.submit_plan(assessed["id"],{"content":"p1","basis_record_ids":[rec["id"]]},"eng",'structural_engineer')
        with self.assertRaises(ConflictError):
            self.service.submit_plan(assessed["id"],{"content":"p2","basis_record_ids":[rec["id"]]},"eng",'structural_engineer')
        with self.assertRaises(ValidationError):
            self.service.review_plan(plan["id"],{"decision":"rejected","expected_version":1},"board",'review_board')
        reviewed=self.service.review_plan(plan["id"],{"decision":"rejected","expected_version":1,"reject_reason":"措施不充分"},"board",'review_board')
        self.assertIn("措施不充分",reviewed["blocking_reason"])
        detail=self.service.get_item(assessed["id"],"viewer")["reinforcement"]
        self.assertEqual(detail["reviewed_by"],"board"); self.assertEqual(detail["current_version"],1)
        # 驳回后可以换版
        plan2=self.service.submit_plan(assessed["id"],{"content":"p2","basis_record_ids":[rec["id"]]},"eng",'structural_engineer')
        self.assertEqual(plan2["version"],2)
if __name__=="__main__": unittest.main()
