"""HTTP-level integration checks. Uses only Python standard library."""
import os, sys, tempfile, threading, unittest, urllib.request, urllib.error, json, base64
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,ROOT)
import server
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); server.DATA_DIR=cls.tmp.name; server.DB_PATH=os.path.join(cls.tmp.name,'http.sqlite3'); server.init_db()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler); cls.port=cls.http.server_address[1]
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls): cls.http.shutdown(); cls.http.server_close(); cls.tmp.cleanup()
    def request(self,path,method='GET',body=None,token=None):
        headers={}
        data=None
        if body is not None: data=json.dumps(body).encode(); headers['Content-Type']='application/json'
        if token: headers['X-Session-Token']=token
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',data=data,headers=headers,method=method)
        with urllib.request.urlopen(req,timeout=10) as r:
            content=r.read()
            return r.status,content,r.headers
    def test_login_users_schedule_and_backup(self):
        status,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        self.assertEqual(status,200); token=json.loads(content)['token']
        status,content,_=self.request('/api/users',token=token); self.assertEqual(status,200); self.assertEqual(len(json.loads(content)['users']),7)
        status,content,_=self.request('/api/schedule/generate','POST',{'date':'2026-10-09'},token)
        self.assertEqual(status,200); generated=json.loads(content); self.assertTrue(generated['ok']); self.assertEqual(len(generated['result']['breaks']),6)
        status,content,_=self.request('/api/schedule/confirm','POST',{'run_id':generated['run_id']},token); self.assertEqual(status,200)
        status,content,headers=self.request('/api/backup','POST',{},token); self.assertEqual(status,200); self.assertIn(b'SQLite format 3',content[:20])
        status,content,_=self.request('/api/restore','POST',{'backup_base64':base64.b64encode(content).decode()},token); self.assertEqual(status,200)
    def test_operator_and_demand_crud_and_ten_minute_validation(self):
        _,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        token=json.loads(content)['token']
        status,content,_=self.request('/api/users','POST',{'username':'crud_operator','display_name':'CRUD Operator','password':'TestPassword123','role':'operator','skills':['Consumer'],'shift_start':'09:10','shift_end':'17:50'},token)
        self.assertEqual(status,201); uid=json.loads(content)['id']
        status,content,_=self.request(f'/api/users/{uid}','PUT',{'username':'crud_operator2','display_name':'Edited Operator','password':'','skills':['Consumer','PP'],'shift_start':'09:20','shift_end':'17:40'},token)
        self.assertEqual(status,200)
        users=json.loads(self.request('/api/users',token=token)[1])['users']; user=next(x for x in users if x['id']==uid)
        self.assertEqual(user['display_name'],'Edited Operator'); self.assertEqual(user['shift_start'],'09:20')
        self.request(f'/api/users/{uid}','DELETE',token=token)
        users=json.loads(self.request('/api/users',token=token)[1])['users']; self.assertFalse(next(x for x in users if x['id']==uid)['active'])
        self.request(f'/api/users/{uid}','PUT',{'active':True},token)
        users=json.loads(self.request('/api/users',token=token)[1])['users']; self.assertTrue(next(x for x in users if x['id']==uid)['active'])
        status,content,_=self.request('/api/demand','POST',{'skill':'Consumer','start_time':'10:10','end_time':'11:20','min_staff':2},token)
        self.assertEqual(status,200)
        demands=json.loads(self.request('/api/demand',token=token)[1])['demand']; row=next(x for x in demands if x['skill']=='Consumer' and x['start_time']=='10:10')
        status,content,_=self.request(f'/api/demand/{row["id"]}','PUT',{'skill':'Consumer','start_time':'10:20','end_time':'11:30','min_staff':3},token)
        self.assertEqual(status,200)
        demands=json.loads(self.request('/api/demand',token=token)[1])['demand']; row=next(x for x in demands if x['id']==row['id'])
        self.assertEqual(row['min_staff'],3); self.assertEqual(row['start_time'],'10:20')
        self.request(f'/api/demand/{row["id"]}','DELETE',token=token)
        demands=json.loads(self.request('/api/demand',token=token)[1])['demand']; self.assertFalse(any(x['id']==row['id'] for x in demands))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/api/demand','POST',{'skill':'PP','start_time':'10:05','end_time':'11:00','min_staff':1},token)
        self.assertEqual(ctx.exception.code,400)
    def test_attendance_is_saved_and_absent_operators_are_excluded(self):
        _,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        token=json.loads(content)['token']; date='2026-10-10'
        status,content,_=self.request('/api/attendance?date='+date,token=token)
        self.assertEqual(status,200); operators=json.loads(content)['operators']
        self.assertTrue(all(x['present'] for x in operators))
        absent_id=next(x['id'] for x in operators if x['username']=='operator2')
        payload=[{'user_id':x['id'],'present':x['id']!=absent_id} for x in operators]
        status,content,_=self.request('/api/attendance','POST',{'date':date,'attendance':payload},token)
        self.assertEqual(status,200)
        status,content,_=self.request('/api/attendance?date='+date,token=token)
        saved=json.loads(content)['operators']; self.assertFalse(next(x for x in saved if x['id']==absent_id)['present'])
        # For this focused exclusion test, remove staffing demand so the heuristic can
        # produce a schedule while we verify that the absent user is never assigned.
        con=server.db(); con.execute('UPDATE demand SET min_staff=0'); con.commit(); con.close()
        status,content,_=self.request('/api/schedule/generate','POST',{'date':date},token)
        self.assertEqual(status,200); result=json.loads(content)['result']
        con=server.db(); con.execute('UPDATE demand SET min_staff=1'); con.commit(); con.close()
        self.assertIn(absent_id,[x['id'] for x in result['attendance']['absent']])
        self.assertNotIn(absent_id,{x['operator_id'] for x in result['schedule']})
        self.assertNotIn(str(absent_id),result['breaks'])
    def test_manual_schedule_creates_draft_and_checks_staffing(self):
        _,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        token=json.loads(content)['token']; date='2026-10-11'
        con=server.db(); con.execute('UPDATE demand SET min_staff=0'); con.commit(); con.close()
        _,content,_=self.request('/api/attendance?date='+date,token=token)
        operators=json.loads(content)['operators']
        payload={'date':date,'breaks':[{'user_id':u['id'],'morning':'09:30','lunch':'11:30','afternoon':'14:00'} for u in operators]}
        status,content,_=self.request('/api/schedule/manual','POST',payload,token)
        self.assertEqual(status,200); result=json.loads(content); self.assertTrue(result['ok'])
        self.assertEqual(len(result['result']['breaks']),len(operators))
        con=server.db(); con.execute('UPDATE demand SET min_staff=1'); con.commit(); con.close()
        bad={'date':'2026-10-12','breaks':[{'user_id':u['id'],'morning':'09:30','lunch':'11:30','afternoon':'14:00'} for u in operators]}
        status,content,_=self.request('/api/schedule/manual','POST',bad,token)
        self.assertEqual(status,200); warning_plan=json.loads(content); self.assertTrue(warning_plan['ok'])
        self.assertTrue(warning_plan['result']['shortages'])
        run_id=warning_plan['run_id']
        # Stage two cannot be used without the first explicit acknowledgement.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/api/schedule/confirm','POST',{'run_id':run_id,'override_stage':2},token)
        self.assertEqual(ctx.exception.code,409)
        status,content,_=self.request('/api/schedule/confirm','POST',{'run_id':run_id,'override_stage':1},token)
        self.assertTrue(json.loads(content)['needs_second_confirmation'])
        status,content,_=self.request('/api/schedule/confirm','POST',{'run_id':run_id,'override_stage':2},token)
        self.assertEqual(status,200); self.assertTrue(json.loads(content)['override'])
        con=server.db(); row=con.execute('SELECT status,result_json FROM schedule_runs WHERE id=?',(run_id,)).fetchone(); con.close()
        self.assertEqual(row['status'],'confirmed'); self.assertIn('override_stage2_by',json.loads(row['result_json']))
    def test_z_multi_admin_account_creation_and_permissions(self):
        _,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        owner_token=json.loads(content)['token']
        status,content,_=self.request('/api/users','POST',{'username':'second_admin','display_name':'Second Admin','password':'SecurePass123!','role':'admin','skills':[],'shift_start':'09:00','shift_end':'18:00'},owner_token)
        self.assertEqual(status,201)
        users=json.loads(self.request('/api/users',token=owner_token)[1])['users']
        new_admin=next(u for u in users if u['username']=='second_admin')
        self.assertEqual(new_admin['role'],'admin')
        _,content,_=self.request('/api/login','POST',{'username':'second_admin','password':'SecurePass123!'})
        second_token=json.loads(content)['token']
        self.assertEqual(self.request('/api/users',token=second_token)[0],200)
        _,content,_=self.request('/api/login','POST',{'username':'operator1','password':'Welcome123!'})
        operator_token=json.loads(content)['token']
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/api/users',token=operator_token)
        self.assertEqual(ctx.exception.code,403)
        html=open(os.path.join(ROOT,'static','index.html'),encoding='utf-8').read()
        self.assertIn('name="role" id="userRole"',html)
        self.assertIn('管理者権限も付与',html)
    def test_password_policy_on_creation_and_change(self):
        _,content,_=self.request('/api/login','POST',{'username':'admin','password':'ChangeMe123!'})
        token=json.loads(content)['token']
        base={'username':'pw_policy_user','display_name':'Password Policy User','role':'operator','skills':['Consumer'],'shift_start':'09:00','shift_end':'18:00'}
        for password in ('abcdefg1', 'Abcdefghijklmnop1!', 'Abcdefgh', '12345678'):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request('/api/users','POST',{**base,'password':password},token=token)
            self.assertEqual(ctx.exception.code,400)
        status,content,_=self.request('/api/users','POST',{**base,'password':'GoodPass1!'},token=token)
        self.assertEqual(status,201)
        _,content,_=self.request('/api/login','POST',{'username':'pw_policy_user','password':'GoodPass1!'})
        user_token=json.loads(content)['token']
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/api/password','POST',{'old_password':'GoodPass1!','new_password':'abcdefgh1'},token=user_token)
        self.assertEqual(ctx.exception.code,400)
        status,content,_=self.request('/api/password','POST',{'old_password':'GoodPass1!','new_password':'StrongPass1!'},token=user_token)
        self.assertEqual(status,200)
        html=open(os.path.join(ROOT,'static','index.html'),encoding='utf-8').read()
        self.assertIn('data-toggle-password',html)
        self.assertIn('maxlength="16"',html)
    def test_wrong_password_fails(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx: self.request('/api/login','POST',{'username':'admin','password':'incorrect'})
        self.assertEqual(ctx.exception.code,401)
if __name__=='__main__': unittest.main(verbosity=2)
