"""Regression checks for break-only standalone app (no monthly shift planner)."""
import os, sys, tempfile, unittest
from unittest import mock
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

class BreakRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        server.DB_PATH=os.path.join(self.temp.name,'break_test.sqlite3')
        server.init_db()
        con=server.db()
        con.execute("UPDATE demand SET min_staff=0")
        con.execute("UPDATE users SET active=0 WHERE role='operator'")
        con.commit();con.close()

    def tearDown(self): self.temp.cleanup()

    def person(self,start,end,kind='auto'):
        con=server.db(); uid=con.execute("SELECT id FROM users WHERE username='operator1'").fetchone()['id']
        con.execute('UPDATE users SET shift_start=?,shift_end=?,shift_type=?,active=1 WHERE id=?',(start,end,kind,uid))
        con.commit();u=server.as_user(con.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone());con.close()
        return u

    def manual(self,u,morning='',lunch='',afternoon=''):
        return server.create_manual_schedule('2026-10-13',1,[{'user_id':u['id'],'morning':morning,'lunch':lunch,'afternoon':afternoon}])

    def test_under_6_hours_one_microbreak(self):
        u=self.person('09:20','15:10','その他')
        self.assertEqual([x[0] for x in server.required_break_specs(u)], ['前半10分休憩'])
        result=server.generate_schedule('2026-10-13',1)
        self.assertTrue(result['ok'],result.get('errors'))
        self.assertEqual(len(result['result']['breaks'][str(u['id'])]),1)

    def test_six_exactly_two_ten_minute_breaks(self):
        u=self.person('09:20','15:20','その他')
        self.assertEqual(len(server.required_break_specs(u)),2)
        self.assertTrue(server.generate_schedule('2026-10-13',1)['ok'])
        self.assertTrue(self.manual(u,morning='10:20',afternoon='13:30')['ok'])
        self.assertFalse(self.manual(u,morning='10:20',lunch='12:00',afternoon='13:30')['ok'])

    def test_over_six_requires_lunch_even_when_not_regular_shift(self):
        u=self.person('10:00','17:00','その他')
        self.assertEqual(len(server.required_break_specs(u)),3)
        result=server.generate_schedule('2026-10-13',1)
        self.assertTrue(result['ok'], result.get('errors'))
        kinds={x['type'] for x in result['result']['breaks'][str(u['id'])]}
        self.assertEqual(kinds,{'morning','lunch','afternoon'})
        self.assertFalse(self.manual(u,morning='11:00',afternoon='15:00')['ok'])
        self.assertTrue(self.manual(u,morning='11:00',lunch='12:10',afternoon='15:00')['ok'])

    def test_early_lunch_first_only_1130(self):
        u=self.person('09:20','18:20','早番')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][0],'11:30')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][-1],'14:00')
        self.assertTrue(self.manual(u,lunch='11:30',morning='12:50',afternoon='15:00')['ok'])
        self.assertFalse(self.manual(u,lunch='11:40',morning='13:00',afternoon='15:00')['ok'])

    def test_early_1130_requires_morning_after_lunch_in_candidates(self):
        u=self.person('09:20','18:20','早番')
        p=server.employee_as_person(u)
        meal=11*60+30
        mornings=server.flex._break_candidates(p,'前半10分休憩',10,meal)
        self.assertTrue(mornings)
        self.assertEqual(server.flex.clock(mornings[0]),'12:50')
        self.assertTrue(all(x>=12*60+50 for x in mornings))
        self.assertFalse(server.flex.valid_break_sequence(p,{'morning':10*60+20,'lunch':meal,'afternoon':15*60}))
        self.assertTrue(server.flex.valid_break_sequence(p,{'morning':12*60+50,'lunch':meal,'afternoon':15*60}))
        # Lunch at 11:30 disallows a pre-lunch morning break even if the
        # remainder of the submitted plan satisfies gap/shift constraints.
        bad=self.manual(u,morning='10:20',lunch='11:30',afternoon='15:00')
        self.assertFalse(bad['ok'])
        self.assertIn('11:30',bad['errors'][0])
        self.assertTrue(self.manual(u,morning='12:50',lunch='11:30',afternoon='15:00')['ok'])
        # If lunch is later than 11:30, existing morning-before-lunch order stays.
        self.assertTrue(self.manual(u,morning='10:20',lunch='11:40',afternoon='15:00')['ok'])
        self.assertFalse(self.manual(u,morning='13:00',lunch='11:40',afternoon='15:00')['ok'])

    def test_auto_early_lunch_1130_must_assign_morning_after(self):
        u=self.person('09:20','18:20','早番')
        orig=server.flex._break_candidates
        def lunch_fixed(person,name,duration,lunch_start=None,first_start=None):
            if name=='昼休憩' and server.flex.shift_kind(person)=='早番':
                return [11*60+30]
            return orig(person,name,duration,lunch_start,first_start)
        with mock.patch.object(server.flex,'_break_candidates',side_effect=lunch_fixed):
            response=server.generate_schedule('2026-10-13',1)
        self.assertTrue(response['ok'],response.get('errors'))
        breaks={row['type']:row for row in response['result']['breaks'][str(u['id'])]}
        self.assertEqual(breaks['lunch']['start'],'11:30')
        self.assertGreaterEqual(server.mins(breaks['morning']['start']),12*60+50)
        self.assertGreaterEqual(server.mins(breaks['afternoon']['start']),
                                server.mins(breaks['morning']['end'])+20)

    def test_old_draft_early_1130_with_morning_before_cannot_be_confirmed(self):
        u=self.person('09:20','18:20','早番')
        result=server.generate_schedule('2026-10-13',1)
        self.assertTrue(result['ok'],result.get('errors'))
        self.assertTrue(server.validate_saved_breaks(result['result']))
        result['result']['breaks'][str(u['id'])]=[
            {'type':'morning','start':'10:20','end':'10:30'},
            {'type':'lunch','start':'11:30','end':'12:30'},
            {'type':'afternoon','start':'15:00','end':'15:10'}]
        self.assertFalse(server.validate_saved_breaks(result['result']))

    def test_early_manual_dropdown_filters_1130_choices(self):
        self.person('09:20','18:20','早番')
        with open(os.path.join(ROOT,'static','index.html'),encoding='utf-8') as source:
            page=source.read()
        self.assertIn('function syncEarlyManualBreakChoices(uid)',page)
        self.assertIn("const firstAfterLunch=lunch.value==='11:30'",page)
        self.assertIn('minute(t)>=lunchEnd+20',page)

    def test_middle_must_take_morning_before_lunch(self):
        u=self.person('11:00','20:00','中番')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][0],'13:10')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][-1],'15:40')
        self.assertFalse(self.manual(u,lunch='13:10',morning='14:30',afternoon='17:00')['ok'])
        self.assertTrue(self.manual(u,morning='12:00',lunch='13:10',afternoon='17:00')['ok'])

    def test_late_lunch_first_and_evening_coverage(self):
        u=self.person('14:00','23:00','遅番')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][0],'16:10')
        self.assertEqual(server.manual_break_options(u)['options']['lunch'][-1],'18:40')
        self.assertTrue(self.manual(u,lunch='16:10',morning='17:30',afternoon='20:00')['ok'])
        result=server.generate_schedule('2026-10-13',1)
        self.assertTrue(result['ok'],result.get('errors'))
        self.assertIn('22:50',[row['time'] for row in result['result']['schedule']])

    def test_old_draft_without_lunch_is_not_confirmable(self):
        u=self.person('09:20','18:20','早番')
        result=server.generate_schedule('2026-10-13',1)
        self.assertTrue(server.validate_saved_breaks(result['result']))
        result['result']['breaks'][str(u['id'])]=[{'type':'morning','start':'10:20','end':'10:30'}]
        self.assertFalse(server.validate_saved_breaks(result['result']))

    def test_all_day_midshift_1850_limit(self):
        u=self.person('16:00','23:00','遅番')
        opts=server.manual_break_options(u)['options']['lunch']
        self.assertLessEqual(opts[-1],'18:50')

if __name__=='__main__':unittest.main(verbosity=2)
