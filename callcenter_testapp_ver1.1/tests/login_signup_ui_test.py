"""Self-registration UI requires account data only; admin retains shift pattern controls."""
import json, os, sys, tempfile, threading, unittest, urllib.request, urllib.error
from html.parser import HTMLParser
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server

class Forms(HTMLParser):
    def __init__(self):
        super().__init__(); self.in_form=None; self.inputs={}
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='form': self.in_form=a.get('id')
        if self.in_form and tag in ('input','select'):
            self.inputs.setdefault(self.in_form,[]).append(a.get('name'))
    def handle_endtag(self, tag):
        if tag=='form': self.in_form=None

class LoginSignupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.old_db=server.DB_PATH
        server.DB_PATH=os.path.join(cls.tmp.name,'signup.sqlite3'); server.init_db()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.port=cls.http.server_address[1]
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); server.DB_PATH=cls.old_db; cls.tmp.cleanup()
    def request(self,path,body,token=None):
        headers={'Content-Type':'application/json'}
        if token: headers['X-Session-Token']=token
        req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+path,data=json.dumps(body).encode(),headers=headers,method='POST')
        try:
            with urllib.request.urlopen(req,timeout=10) as r:
                return r.status,json.loads(r.read())
        except urllib.error.HTTPError as e: return e.code,json.loads(e.read())
    def test_signup_ui_has_no_shift_fields(self):
        page=open(os.path.join(ROOT,'static','index.html'),encoding='utf-8').read()
        p=Forms();p.feed(page)
        signup=p.inputs['signupForm']; admin=p.inputs['userForm']
        self.assertTrue({'username','display_name','password','password_confirm','skills'} <= set(signup))
        self.assertTrue({'shift_start','shift_end','shift_type','work_pattern_id'}.isdisjoint(signup))
        self.assertTrue({'shift_start','shift_end','shift_type','work_pattern_id'} <= set(admin))
        js=page[page.index("$('signupForm').addEventListener"):page.index("$('loginForm').addEventListener")]
        for key in ('shift_start:', 'shift_end:', 'shift_type:', 'work_pattern_id:'):
            self.assertNotIn(key,js)
    def test_signup_without_working_hours_and_later_admin_assignment(self):
        data={'username':'signup_missing_shift','display_name':'Registration Tester','password':'Example123!','skills':['Consumer','PP']}
        status,reply=self.request('/api/register',data)
        self.assertEqual(status,201,reply)
        status,_=self.request('/api/login',{'username':data['username'],'password':data['password']})
        self.assertEqual(status,401)  # Admin approval remains necessary.
        status,auth=self.request('/api/login',{'username':'admin','password':'ChangeMe123!'})
        self.assertEqual(status,200,auth); token=auth['token']
        req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+'/api/users',headers={'X-Session-Token':token})
        with urllib.request.urlopen(req,timeout=10) as r: users=json.loads(r.read())['users']
        user=next(x for x in users if x['username']==data['username'])
        self.assertEqual((user['shift_start'],user['shift_end'],user['shift_type'],user['work_pattern_id'],user['active']),('09:00','18:00','auto',None,0))
        status,pattern=self.request('/api/work-patterns',{'name':'Test Late','start_time':'14:00','end_time':'23:00','shift_type':'遅番'},token)
        self.assertEqual(status,201,pattern)
        req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+'/api/users/'+str(user['id']),data=json.dumps({**data,'role':'operator','password':'','work_pattern_id':pattern['id']}).encode(),headers={'X-Session-Token':token,'Content-Type':'application/json'},method='PUT')
        with urllib.request.urlopen(req,timeout=10) as r: self.assertEqual(r.status,200)
        req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+'/api/users',headers={'X-Session-Token':token})
        with urllib.request.urlopen(req,timeout=10) as r: users=json.loads(r.read())['users']
        user=next(x for x in users if x['username']==data['username'])
        self.assertEqual((user['shift_start'],user['shift_end'],user['shift_type'],user['work_pattern_id']),('14:00','23:00','遅番',pattern['id']))

if __name__=='__main__': unittest.main()
