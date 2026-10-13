"""Validate work-pattern CRUD, assignment, edit propagation, detachment and migration."""
import json, os, sqlite3, sys, tempfile, threading, unittest, urllib.request, urllib.error
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

class WorkPatternsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(); cls.old_db=server.DB_PATH
        server.DB_PATH=os.path.join(cls.temp.name,'patterns.sqlite3'); server.init_db()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.port=cls.http.server_address[1]
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); server.DB_PATH=cls.old_db; cls.temp.cleanup()
    def request(self,path,method='GET',body=None,token=None,expected=200):
        headers={'Content-Type':'application/json'}
        if token:headers['X-Session-Token']=token
        req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+path,
            data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=10) as res:
                status=res.status; payload=json.loads(res.read())
        except urllib.error.HTTPError as error:
            status=error.code;payload=json.loads(error.read())
        self.assertEqual(status,expected,(method,path,status,payload));return payload
    def login(self,user,password):
        return self.request('/api/login','POST',{'username':user,'password':password})['token']
    def test_pattern_crud_assignment_and_breaks(self):
        admin=self.login('admin','ChangeMe123!')
        operator=self.login('operator1','Welcome123!')
        self.request('/api/work-patterns',token=operator,expected=403)
        self.request('/api/work-patterns', 'POST',{'name':'X','start_time':'09:20','end_time':'18:20'},operator,expected=403)
        self.request('/api/work-patterns','POST',{'name':'bad','start_time':'09:25','end_time':'18:20','shift_type':'早番'},admin,expected=400)
        d=self.request('/api/work-patterns','POST',{'name':'早番A','start_time':'09:20','end_time':'18:20','shift_type':'早番'},admin,expected=201)
        pid=d['id']
        self.request('/api/work-patterns','POST',{'name':'早番A','start_time':'09:20','end_time':'18:20','shift_type':'早番'},admin,expected=409)
        user_payload={'username':'pattern_operator','display_name':'勤務パターン担当','password':'Example123!','role':'operator','skills':['Consumer'],'shift_start':'10:00','shift_end':'17:00','shift_type':'その他','work_pattern_id':pid}
        uid=self.request('/api/users','POST',user_payload,admin,expected=201)['id']
        users=self.request('/api/users',token=admin)['users']
        u=next(u for u in users if u['id']==uid)
        self.assertEqual((u['work_pattern_id'],u['shift_start'],u['shift_end'],u['shift_type']), (pid,'09:20','18:20','早番'))
        attendance=self.request('/api/attendance?date=2026-10-13',token=admin)['operators']
        a=next(u for u in attendance if u['id']==uid)
        self.assertEqual((a['shift_start'],a['shift_end'],a['break_editor']['shift_kind']),('09:20','18:20','早番'))
        # Changing the pattern updates every linked operator's effective working hours.
        result=self.request('/api/work-patterns/'+str(pid),'PUT',{'name':'中番A','start_time':'11:00','end_time':'20:00','shift_type':'中番'},admin)
        self.assertEqual(result['updated_operators'],1)
        u=next(u for u in self.request('/api/users',token=admin)['users'] if u['id']==uid)
        self.assertEqual((u['shift_start'],u['shift_end'],u['shift_type']),('11:00','20:00','中番'))
        opts=next(u for u in self.request('/api/attendance?date=2026-10-13',token=admin)['operators'] if u['id']==uid)['break_editor']
        self.assertEqual(opts['shift_kind'],'中番')
        self.assertTrue(opts['options']['lunch'])
        self.assertTrue(all(x >= '13:10' for x in opts['options']['lunch']))
        # Invalid stale/deleted pattern IDs are rejected.
        self.request('/api/users/'+str(uid),'PUT',dict(user_payload,password='',work_pattern_id=777777),admin,expected=400)
        # Admin may explicitly detach by choosing manual schedule.
        self.request('/api/users/'+str(uid),'PUT',dict(user_payload,password='',shift_start='09:20',shift_end='18:20',work_pattern_id=None),admin)
        u=next(u for u in self.request('/api/users',token=admin)['users'] if u['id']==uid)
        self.assertIsNone(u['work_pattern_id']);self.assertEqual(u['shift_start'],'09:20')
        # Reassignment on edit works too.
        self.request('/api/users/'+str(uid),'PUT',dict(user_payload,password='',work_pattern_id=pid),admin)
        self.assertEqual(next(x for x in self.request('/api/work-patterns',token=admin)['patterns'] if x['id']==pid)['assigned_count'],1)
        result=self.request('/api/work-patterns/'+str(pid),'DELETE',token=admin)
        self.assertEqual(result['detached_operators'],1)
        u=next(u for u in self.request('/api/users',token=admin)['users'] if u['id']==uid)
        self.assertIsNone(u['work_pattern_id']);self.assertEqual((u['shift_start'],u['shift_end']),('11:00','20:00'))
        self.request('/api/users/'+str(uid),'PUT',dict(user_payload,password='',work_pattern_id=pid),admin,expected=400)
    def test_legacy_database_migration(self):
        # A copy of the old DB structure has no pattern column yet.
        tmp=tempfile.TemporaryDirectory(); path=os.path.join(tmp.name,'old.sqlite3')
        con=sqlite3.connect(path)
        con.execute("""CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER DEFAULT 1,skills TEXT DEFAULT '[]',shift_start TEXT DEFAULT '09:00',shift_end TEXT DEFAULT '18:00',shift_type TEXT DEFAULT 'auto',created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("INSERT INTO users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end,shift_type) VALUES('old1','Old employee','operator','s','hash','[]','09:20','18:20','早番')")
        con.commit();con.close()
        old=server.DB_PATH
        try:
            server.DB_PATH=path;server.init_db();server.init_db()
            con=server.db();row=con.execute('SELECT * FROM users WHERE username=?',('old1',)).fetchone()
            self.assertEqual((row['shift_start'],row['work_pattern_id']),('09:20',None))
            self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE name='work_patterns'").fetchone());con.close()
        finally:server.DB_PATH=old;tmp.cleanup()

if __name__=='__main__': unittest.main()
