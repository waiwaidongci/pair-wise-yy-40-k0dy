import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"failure item","description":"failure scenarios","severity":'high',"quantity":5,"threshold":10,"external_ref":"FAIL-1"},"creator",'assessor')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_permission_version_duplicate_and_invariant(self):
        with self.assertRaises(PermissionDenied): self.service.transition(self.item["id"],STATES[1],1,"attacker","viewer")
        with self.assertRaises(ConflictError): self.service.transition(self.item["id"],STATES[1],99,"reviewer",TRANSITION_ROLES[STATES[1]][0])
        payload={"kind":"action","detail":"same reference","status":"open","external_ref":"DUP-1"}
        self.service.add_record(self.item["id"],payload,"recorder",'assessor')
        with self.assertRaises(ConflictError): self.service.add_record(self.item["id"],payload,"recorder",'assessor')
        closed=self.service.add_record(self.item["id"],{"kind":"evidence","detail":"closed basis","status":"closed","external_ref":"CL-1"},"recorder",'assessor')
        current=self.service.transition(self.item["id"],STATES[1],self.item["version"],"reviewer",TRANSITION_ROLES[STATES[1]][0])
        plan=self.service.submit_plan(current["id"],{"content":"plan","record_ids":[closed["id"]]},"engineer",'structural_engineer')
        self.service.review_plan(plan["id"],{"decision":"approve"},"board",'review_board')
        for target in STATES[2:4]: current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError): self.service.transition(current["id"],"accepted",current["version"],"reviewer",TRANSITION_ROLES["accepted"][0])
if __name__=="__main__": unittest.main()
