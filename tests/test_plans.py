import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.repo=Repository(str(Path(self.tmp.name)/"test.db"))
        self.service=Service(self.repo)
        item=self.service.create_item({"title":"plan item","description":"plan lifecycle","severity":'high',"quantity":5,"threshold":10},"creator",'assessor')
        self.item=self.service.transition(item["id"],"assessed",1,"r",'assessor')
        self.rec=self.service.add_record(self.item["id"],{"kind":"assessment","detail":"closed finding","status":"closed","external_ref":"AS-1"},"r",'assessor')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _plan(self,content="p"):
        return self.service.submit_plan(self.item["id"],{"content":content,"basis_record_ids":[self.rec["id"]]},"eng",'structural_engineer')
    def _approve(self,plan):
        return self.service.review_plan(plan["id"],{"decision":"approved","expected_version":plan["version"],"comment":"ok"},"board",'review_board')
    def test_approve_unlocks_design(self):
        plan=self._approve(self._plan())
        self.assertTrue(plan["is_current"]); self.assertEqual(plan["reviewed_by"],"board")
        self.assertIsNone(plan["blocking_reason"])
        gate=self.service.get_item(self.item["id"],"viewer")["reinforcement"]
        self.assertTrue(gate["design_unlocked"])
        designed=self.service.transition(self.item["id"],"design",self.item["version"],"eng",'structural_engineer')
        self.assertEqual(designed["status"],"design")
    def test_new_version_invalidates_and_regresses_design(self):
        self._approve(self._plan())
        designed=self.service.transition(self.item["id"],"design",self.item["version"],"eng",'structural_engineer')
        v2=self._plan("p2")
        self.assertEqual(v2["version"],2); self.assertEqual(v2["status"],"pending")
        plans=self.service.list_plans(self.item["id"],"viewer")
        self.assertEqual(plans[0]["status"],"invalidated"); self.assertFalse(plans[0]["is_current"])
        self.assertIn("换版",plans[0]["invalid_reason"])
        self.assertIsNone(plans[0]["blocking_reason"])
        item=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(item["status"],"assessed")
        self.assertIn("复核",item["reinforcement"]["blocking_reason"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"],"design",item["version"],"eng",'structural_engineer')
    def test_assessment_added_in_construction_does_not_regress(self):
        self._approve(self._plan())
        current=self.service.transition(self.item["id"],"design",self.item["version"],"eng",'structural_engineer')
        current=self.service.transition(self.item["id"],"construction",current["version"],"eng",'structural_engineer')
        # 已进施工：事后补评估事项，同意失效但状态不倒退
        self.service.add_record(self.item["id"],{"kind":"assessment","detail":"later finding","status":"closed"},"r",'assessor')
        item=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(item["status"],"construction")
        plans=self.service.list_plans(self.item["id"],"viewer")
        self.assertEqual(plans[0]["status"],"invalidated")
    def test_non_assessment_record_does_not_invalidate(self):
        self._approve(self._plan())
        self.service.add_record(self.item["id"],{"kind":"evidence","detail":"photo","status":"closed"},"r",'assessor')
        plans=self.service.list_plans(self.item["id"],"viewer")
        self.assertEqual(plans[0]["status"],"approved")
        self.assertIsNone(plans[0]["blocking_reason"])
    def test_record_update_invalidates_approved_plan(self):
        self._approve(self._plan())
        self.service.update_record(self.rec["id"],{"kind":"assessment","detail":"amended detail","status":"closed"},"r",'assessor')
        plans=self.service.list_plans(self.item["id"],"viewer")
        self.assertEqual(plans[0]["status"],"invalidated")
        self.assertIn("补改",plans[0]["invalid_reason"])
    def test_double_review_blocked_by_version(self):
        plan=self._plan()
        self._approve(plan)
        with self.assertRaises(ConflictError):
            self.service.review_plan(plan["id"],{"decision":"rejected","expected_version":1,"reject_reason":"x"},"board",'review_board')
if __name__=="__main__": unittest.main()
