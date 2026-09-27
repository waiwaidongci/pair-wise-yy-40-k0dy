import unittest
from src import rules
from src.domain import ConflictError, ValidationError
class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_plan_rules(self):
        self.assertEqual(rules.design_blockers(None),["尚未提交加固方案"])
        self.assertEqual(rules.design_blockers({"status":"approved"}),[])
        self.assertEqual(rules.design_blockers({"status":"pending"}),["加固方案待评审"])
        self.assertIn("驳回",rules.design_blockers({"status":"rejected","review_reason":"r"})[0])
        self.assertIn("失效",rules.design_blockers({"status":"invalidated","invalidated_reason":"方案换版"})[0])
        self.assertEqual(rules.normalize_decision("approve"),"approved"); self.assertEqual(rules.normalize_decision("reject"),"rejected")
        with self.assertRaises(ValidationError): rules.normalize_decision("hold")
        with self.assertRaises(ValidationError): rules.validate_review("rejected","")
        rules.validate_review("rejected","原因"); rules.validate_review("approved",None)
        with self.assertRaises(ConflictError): rules.validate_plan_submission("proposed")
        rules.validate_plan_submission("assessed")
        self.assertEqual(rules.transition_blockers("design",0,None),["尚未提交加固方案"])
        self.assertEqual(rules.transition_blockers("accepted",1,{"status":"approved"}),["仍有未关闭事项"])
if __name__=="__main__": unittest.main()
