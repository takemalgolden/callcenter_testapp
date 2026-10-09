"""Lightweight offline smoke tests for the standard-library backend."""
import os, tempfile, unittest, importlib.util
HERE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec=importlib.util.spec_from_file_location('ccserver',os.path.join(HERE,'server.py'))
app=importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); app.DATA_DIR=self.tmp.name; app.DB_PATH=os.path.join(self.tmp.name,'test.sqlite3'); app.init_db()
    def tearDown(self): self.tmp.cleanup()
    def test_seed_users_and_demand(self):
        con=app.db(); self.assertEqual(con.execute('select count(*) from users').fetchone()[0],7); self.assertEqual(con.execute('select count(*) from demand').fetchone()[0],3); con.close()
    def test_password_hash(self):
        salt,digest=app.hash_password('example-password'); self.assertTrue(app.verify_password('example-password',salt,digest)); self.assertFalse(app.verify_password('wrong-password',salt,digest))
    def test_skills_are_known(self):
        con=app.db(); rows=con.execute("select skills from users where role='operator'").fetchall(); con.close()
        for row in rows: self.assertTrue(set(__import__('json').loads(row[0])).issubset(set(app.SKILLS)))
if __name__=='__main__': unittest.main(verbosity=2)
