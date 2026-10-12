#!/usr/bin/env python3
"""Call Center Local Scheduler - standard-library LAN server with SQLite."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json, sqlite3, os, secrets, hashlib, hmac, time, threading, socket, sys, base64, tempfile, csv, io, zipfile, re
import xml.etree.ElementTree as ET
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
RESOURCE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))) if getattr(sys, 'frozen', False) else BASE_DIR
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA', BASE_DIR), 'CallCenterLocal', 'data')
else:
    DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'callcenter.sqlite3')
HOST = os.environ.get('CALLCENTER_HOST', '0.0.0.0')
PORT = int(os.environ.get('CALLCENTER_PORT', '8765'))
SKILLS = ['Consumer', 'Merchant', 'PP']
SESSIONS = {}
EVENTS = []
EVENT_LOCK = threading.Lock()

os.makedirs(DATA_DIR, exist_ok=True)

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=15000')
    return con

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 240000).hex()
    return salt, digest

def verify_password(password, salt, digest):
    return hmac.compare_digest(hash_password(password, salt)[1], digest)

def valid_password(password):
    """Require 8-16 chars and at least three of uppercase/lowercase/digit/symbol."""
    if not isinstance(password, str) or not 8 <= len(password) <= 16:
        return False
    categories = (
        any('A' <= c <= 'Z' for c in password),
        any('a' <= c <= 'z' for c in password),
        any('0' <= c <= '9' for c in password),
        any(not (('A' <= c <= 'Z') or ('a' <= c <= 'z') or ('0' <= c <= '9')) for c in password),
    )
    return sum(categories) >= 3

PASSWORD_RULE_MESSAGE = 'パスワードは8〜16文字で、大文字・小文字・数字・記号の4種類のうち3種類以上を含めてください。'

def emit(kind='update'):
    with EVENT_LOCK:
        EVENTS.append({'id': time.time_ns(), 'kind': kind, 'time': time.time()})
        del EVENTS[:-100]

def init_db():
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      display_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','operator')),
      salt TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
      skills TEXT NOT NULL DEFAULT '[]', shift_start TEXT NOT NULL DEFAULT '09:00',
      shift_end TEXT NOT NULL DEFAULT '18:00', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS demand(
      id INTEGER PRIMARY KEY AUTOINCREMENT, skill TEXT NOT NULL,
      start_time TEXT NOT NULL, end_time TEXT NOT NULL, min_staff INTEGER NOT NULL CHECK(min_staff>=0),
      UNIQUE(skill,start_time,end_time)
    );
    CREATE TABLE IF NOT EXISTS attendance(
      attendance_date TEXT NOT NULL, user_id INTEGER NOT NULL, present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_by INTEGER,
      PRIMARY KEY(attendance_date,user_id),
      FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(updated_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS schedule_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_date TEXT NOT NULL,
      status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      created_by INTEGER, FOREIGN KEY(created_by) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL,
      details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    ''')
    count = con.execute('SELECT COUNT(*) n FROM users').fetchone()['n']
    if count == 0:
        salt, digest = hash_password('ChangeMe123!')
        con.execute('INSERT INTO users(username,display_name,role,salt,password_hash,skills) VALUES(?,?,?,?,?,?)',
                    ('admin','Administrator','admin',salt,digest,json.dumps(SKILLS)))
        # Sample data is deliberately small and clearly editable.
        demo = [
          ('operator1','Operator 1',['Consumer','Merchant','PP']),
          ('operator2','Operator 2',['Consumer']),
          ('operator3','Operator 3',['Merchant']),
          ('operator4','Operator 4',['PP']),
          ('operator5','Operator 5',['Consumer','Merchant']),
          ('operator6','Operator 6',['Merchant','PP']),
        ]
        for username, name, skills in demo:
            salt, digest = hash_password('Welcome123!')
            con.execute('INSERT INTO users(username,display_name,role,salt,password_hash,skills) VALUES(?,?,?,?,?,?)',
                        (username,name,'operator',salt,digest,json.dumps(skills)))
        for skill in SKILLS:
            con.execute('INSERT OR IGNORE INTO demand(skill,start_time,end_time,min_staff) VALUES(?,?,?,?)',
                        (skill,'09:00','18:00',1))
    con.commit(); con.close()

def audit(user_id, action, details=None):
    con = db()
    con.execute('INSERT INTO audit_log(user_id,action,details) VALUES(?,?,?)',
                (user_id, action, json.dumps(details or {}, ensure_ascii=False)))
    con.commit(); con.close()

def as_user(row):
    return {'id':row['id'],'username':row['username'],'display_name':row['display_name'],
            'role':row['role'],'active':bool(row['active']),'skills':json.loads(row['skills']),
            'shift_start':row['shift_start'],'shift_end':row['shift_end']}

def mins(t):
    h,m = map(int,t.split(':')); return h*60+m

def valid_time(t):
    try:
        return isinstance(t, str) and len(t)==5 and mins(t)%10==0 and 0<=mins(t)<24*60
    except Exception:
        return False

def hhmm(m): return f'{m//60:02d}:{m%60:02d}'

def generate_schedule(date, actor_id):
    """Conservative 10-minute-slot heuristic. Never publishes an infeasible plan."""
    con = db()
    all_users = [as_user(r) for r in con.execute("SELECT * FROM users WHERE role='operator' AND active=1 ORDER BY id")]
    attendance_rows = {r['user_id']: bool(r['present']) for r in con.execute('SELECT user_id,present FROM attendance WHERE attendance_date=?',(date,))}
    users = [u for u in all_users if attendance_rows.get(u['id'], True)]
    absent_users = [u for u in all_users if not attendance_rows.get(u['id'], True)]
    demands = [dict(r) for r in con.execute('SELECT * FROM demand ORDER BY start_time,skill')]
    con.close()
    if not users:
        return {'ok':False,'errors':['有効なオペレーターが登録されていません。']}
    slots = list(range(9*60,18*60,10))
    # Expand demand rules to slot requirements. Any uncovered time defaults to zero.
    req = {s:{sk:0 for sk in SKILLS} for s in slots}
    for d in demands:
        for s in slots:
            if mins(d['start_time']) <= s and s < mins(d['end_time']):
                req[s][d['skill']] = max(req[s][d['skill']], int(d['min_staff']))
    # Create breaks per person; choose the lowest-impact valid slot, preserving staffing feasibility.
    breaks = {u['id']:[] for u in users}
    occupied = {u['id']:set() for u in users}
    assignments = {s:{} for s in slots}
    # Morning break: 09:30-11:30; afternoon: 14:00-16:30; lunch: 11:30-13:00.
    break_windows = [('morning',570,690,10),('lunch',690,781,60),('afternoon',840,990,10)]
    # For each slot, estimate skill staffing after breaks already assigned and choose breaks with least slack.
    def assign_slot(s):
        avail = [u for u in users if mins(u['shift_start']) <= s and s < mins(u['shift_end']) and s not in occupied[u['id']]]
        # Greedy constrained allocation: fill skills with the least flexible supply first.
        remaining = list(avail); chosen = {}
        for skill in sorted(SKILLS, key=lambda sk: sum(sk in u['skills'] for u in remaining)):
            need = req[s][skill]
            candidates = [u for u in remaining if skill in u['skills']]
            if len(candidates) < need: return None
            candidates.sort(key=lambda u: (sum(x in u['skills'] for x in SKILLS), u['id']))
            for u in candidates[:need]:
                chosen[u['id']] = skill
                remaining.remove(u)
        # Assign remaining people to an available skill, otherwise leave unassigned.
        for u in remaining:
            possible = [sk for sk in SKILLS if sk in u['skills']]
            if possible:
                # Balance counts, but never use one person twice.
                counts = {sk:sum(1 for x in chosen.values() if x==sk) for sk in SKILLS}
                chosen[u['id']] = min(possible, key=lambda sk: counts[sk])
        return chosen
    # Schedule break blocks by urgency and impact, trying all valid positions.
    for u in users:
        for kind, wstart, wend, duration in break_windows:
            candidates = list(range(wstart, wend if kind == 'lunch' else wend-duration+1, 10))
            # Lunch has a 60-minute block, others 10 minutes.
            best = None; best_score = None
            for start in candidates:
                block = list(range(start,start+duration,10))
                if any(s not in slots or s < mins(u['shift_start']) or s >= mins(u['shift_end']) for s in block): continue
                if any(s in occupied[u['id']] for s in block): continue
                # Temporarily mark this operator unavailable and check whether all minima remain feasible.
                for s in block: occupied[u['id']].add(s)
                feasible = True; score = 0
                for s in block:
                    result = assign_slot(s)
                    if result is None:
                        feasible=False; break
                    # Prefer slots with the most staffing slack.
                    score += len(result) - sum(req[s].values()) - 3 * sum(1 for other_id, other_slots in occupied.items() if other_id != u['id'] and s in other_slots)
                for s in block: occupied[u['id']].remove(s)
                if feasible and (best_score is None or score > best_score):
                    best=(start,block); best_score=score
            if best is None:
                # Keep the break rules intact, but choose the valid slot with the smallest staffing deficit.
                fallback = None
                for start_candidate in candidates:
                    block_candidate = list(range(start_candidate, start_candidate + duration, 10))
                    if any(s not in slots or s < mins(u['shift_start']) or s >= mins(u['shift_end']) for s in block_candidate): continue
                    if any(s in occupied[u['id']] for s in block_candidate): continue
                    for ss in block_candidate: occupied[u['id']].add(ss)
                    deficit = sum(sum(max(0, req[ss][sk] - sum(1 for other in users if mins(other['shift_start']) <= ss < mins(other['shift_end']) and ss not in occupied[other['id']] and sk in other['skills'])) for sk in SKILLS) for ss in block_candidate)
                    for ss in block_candidate: occupied[u['id']].remove(ss)
                    candidate_score = (deficit, start_candidate)
                    if fallback is None or candidate_score < fallback[0]: fallback = (candidate_score, start_candidate, block_candidate)
                if fallback is None:
                    return {'ok':False,'errors':[f"{u['display_name']} の {kind}休憩を勤務時間・休憩枠のルール内に配置できません。"], 'shortages':[]}
                best = (fallback[1], fallback[2])
            start, block = best
            for s in block: occupied[u['id']].add(s)
            breaks[u['id']].append({'type':kind,'start':hhmm(start),'end':hhmm(start+duration)})
    # Assign every slot; fail closed if any requirement is unmet.
    schedule=[]; shortages=[]
    for s in slots:
        chosen=assign_slot(s)
        if chosen is None:
            chosen, slot_shortages = assign_slot_partial(users, s, req[s], occupied)
            shortages.extend(slot_shortages)
        for u in users:
            if mins(u['shift_start']) <= s < mins(u['shift_end']):
                schedule.append({'time':hhmm(s),'operator_id':u['id'],'operator':u['display_name'],
                                 'skill':chosen.get(u['id'],'待機'),'on_break':s in occupied[u['id']]})
    result={'date':date,'breaks':breaks,'schedule':schedule,'slots':len(slots),'generated_at':datetime.now().isoformat(timespec='seconds'),
            'attendance':{'present':[{'id':u['id'],'name':u['display_name']} for u in users], 'absent':[{'id':u['id'],'name':u['display_name']} for u in absent_users]},
            'minimums_met': not bool(shortages), 'shortages':shortages[:100],
            'note':'出勤チェック済みのオペレーターのみを休憩・配置計算に含めています。最低稼働人数不足がある場合は警告を確認し、管理者の2段階承認後に限り例外確定できます。休憩枠・勤務時間のルールは緩和しません。'}
    con=db(); cur=con.execute('INSERT INTO schedule_runs(schedule_date,status,result_json,created_by) VALUES(?,?,?,?)',
                              (date,'draft',json.dumps(result,ensure_ascii=False),actor_id)); run_id=cur.lastrowid
    con.commit(); con.close(); audit(actor_id,'schedule_generated',{'run_id':run_id,'date':date}); emit('schedule')
    return {'ok':True,'run_id':run_id,'result':result}

def create_manual_schedule(date, actor_id, submitted_breaks):
    """Validate a user-authored break plan and save it only if every staffing minimum is met."""
    con = db()
    all_users = [as_user(r) for r in con.execute("SELECT * FROM users WHERE role='operator' AND active=1 ORDER BY id")]
    attendance_rows = {r['user_id']: bool(r['present']) for r in con.execute('SELECT user_id,present FROM attendance WHERE attendance_date=?', (date,))}
    users = [u for u in all_users if attendance_rows.get(u['id'], True)]
    absent_users = [u for u in all_users if not attendance_rows.get(u['id'], True)]
    demands = [dict(r) for r in con.execute('SELECT * FROM demand ORDER BY start_time,skill')]
    con.close()
    if not users:
        return {'ok': False, 'errors': ['出勤者がいないため、配置案を作成できません。']}

    by_id = {u['id']: u for u in users}
    if not isinstance(submitted_breaks, list) or {int(x.get('user_id', -1)) for x in submitted_breaks if isinstance(x, dict) and str(x.get('user_id', '')).isdigit()} != set(by_id) or len(submitted_breaks) != len(by_id):
        return {'ok': False, 'errors': ['出勤者全員の午前・昼・午後の休憩時刻を指定してください。']}

    windows = {
        'morning': (570, 690, 10),   # 09:30-11:20 start
        'lunch': (690, 781, 60),     # 11:30-13:00 start
        'afternoon': (840, 981, 10)  # 14:00-16:20 start
    }
    breaks = {u['id']: [] for u in users}
    occupied = {u['id']: set() for u in users}
    slots = list(range(9*60, 18*60, 10))
    for item in submitted_breaks:
        uid = int(item['user_id']); user = by_id[uid]
        for kind in ('morning', 'lunch', 'afternoon'):
            start_text = item.get(kind, '')
            if not valid_time(start_text):
                return {'ok': False, 'errors': [f"{user['display_name']} の休憩時刻が10分刻みではありません。"]}
            start = mins(start_text); wstart, wstop, duration = windows[kind]
            if not (wstart <= start < wstop):
                return {'ok': False, 'errors': [f"{user['display_name']} の{ {'morning':'午前休憩','lunch':'昼休憩','afternoon':'午後休憩'}[kind] }は設定された時間帯から選択してください。"]}
            block = set(range(start, start + duration, 10))
            if any(s not in slots or s < mins(user['shift_start']) or s >= mins(user['shift_end']) for s in block):
                return {'ok': False, 'errors': [f"{user['display_name']} の休憩が勤務時間外に設定されています。"]}
            if occupied[uid] & block:
                return {'ok': False, 'errors': [f"{user['display_name']} の休憩時間が重複しています。"]}
            occupied[uid].update(block)
            breaks[uid].append({'type': kind, 'start': start_text, 'end': hhmm(start + duration)})

    req = {s: {sk: 0 for sk in SKILLS} for s in slots}
    for d in demands:
        for s in slots:
            if mins(d['start_time']) <= s < mins(d['end_time']):
                req[s][d['skill']] = max(req[s][d['skill']], int(d['min_staff']))

    schedule=[]; shortages=[]
    for s in slots:
        available = [u for u in users if mins(u['shift_start']) <= s < mins(u['shift_end']) and s not in occupied[u['id']]]
        remaining = list(available); chosen = {}; slot_infeasible = False
        for skill in sorted(SKILLS, key=lambda sk: sum(sk in u['skills'] for u in remaining)):
            need = req[s][skill]
            candidates = [u for u in remaining if skill in u['skills']]
            if len(candidates) < need:
                slot_infeasible = True
                continue
            candidates.sort(key=lambda u: (sum(x in u['skills'] for x in SKILLS), u['id']))
            for u in candidates[:need]:
                chosen[u['id']] = skill
                remaining.remove(u)
        if slot_infeasible:
            # Assign all possible people once, while retaining a visible minimum-staffing warning.
            chosen, slot_shortages = assign_slot_partial(users, s, req[s], occupied)
            shortages.extend(slot_shortages)
            remaining = [u for u in available if u['id'] not in chosen]
        for u in remaining:
            possible = [sk for sk in SKILLS if sk in u['skills']]
            if possible:
                counts = {sk: sum(1 for x in chosen.values() if x == sk) for sk in SKILLS}
                chosen[u['id']] = min(possible, key=lambda sk: counts[sk])
        for u in users:
            if mins(u['shift_start']) <= s < mins(u['shift_end']):
                schedule.append({'time': hhmm(s), 'operator_id': u['id'], 'operator': u['display_name'],
                                 'skill': chosen.get(u['id'], '待機'), 'on_break': s in occupied[u['id']]})
    result = {'date': date, 'breaks': {str(k): v for k, v in breaks.items()}, 'schedule': schedule,
              'slots': len(slots), 'generated_at': datetime.now().isoformat(timespec='seconds'),
              'attendance': {'present': [{'id': u['id'], 'name': u['display_name']} for u in users],
                            'absent': [{'id': u['id'], 'name': u['display_name']} for u in absent_users]},
              'minimums_met': not bool(shortages), 'shortages': shortages[:100],
              'note': '管理者が手動指定した休憩時刻を検証した案です。最低稼働人数不足がある場合は警告を確認し、管理者の2段階承認後に限り例外確定できます。休憩枠・勤務時間のルールは緩和しません。'}
    con = db()
    cur = con.execute('INSERT INTO schedule_runs(schedule_date,status,result_json,created_by) VALUES(?,?,?,?)',
                       (date, 'draft', json.dumps(result, ensure_ascii=False), actor_id))
    run_id = cur.lastrowid
    con.commit(); con.close()
    audit(actor_id, 'manual_schedule_generated', {'run_id': run_id, 'date': date})
    emit('schedule')
    return {'ok': True, 'run_id': run_id, 'result': result}

def assign_slot_partial(users, slot, requirements, occupied):
    """Build the best possible single-skill assignment and return explicit unmet minima."""
    available = [u for u in users if mins(u['shift_start']) <= slot < mins(u['shift_end']) and slot not in occupied[u['id']]]
    remaining = list(available); chosen = {}; shortages = []
    for skill in sorted(SKILLS, key=lambda sk: sum(sk in u['skills'] for u in remaining)):
        need = int(requirements.get(skill, 0))
        candidates = [u for u in remaining if skill in u['skills']]
        candidates.sort(key=lambda u: (sum(x in u['skills'] for x in SKILLS), u['id']))
        for u in candidates[:need]:
            chosen[u['id']] = skill; remaining.remove(u)
        if len(candidates) < need:
            shortages.append({'time': hhmm(slot), 'skill': skill, 'required': need, 'eligible_available': len(candidates), 'shortage': need-len(candidates)})
    for u in remaining:
        possible = [sk for sk in SKILLS if sk in u['skills']]
        if possible:
            counts = {sk: sum(1 for x in chosen.values() if x == sk) for sk in SKILLS}
            chosen[u['id']] = min(possible, key=lambda sk: counts[sk])
    return chosen, shortages

def find_shortages(users, slots, req, occupied):
    out=[]
    for s in slots:
        available=[u for u in users if mins(u['shift_start'])<=s<mins(u['shift_end']) and s not in occupied[u['id']]]
        for skill in SKILLS:
            have=sum(1 for u in available if skill in u['skills'])
            if have<req[s][skill]: out.append({'time':hhmm(s),'skill':skill,'required':req[s][skill],'eligible_available':have})
    return out[:30]

def parse_employee_file(filename, payload):
    """Read first worksheet from .xlsx or CSV, no external dependencies."""
    if len(payload)>2_000_000: raise ValueError('ファイルは2MB以下にしてください。')
    lower=filename.lower()
    if lower.endswith('.csv'):
        content=None
        for encoding in ('utf-8-sig','cp932'):
            try: content=payload.decode(encoding); break
            except UnicodeDecodeError: pass
        if content is None: raise ValueError('CSVの文字コードを読み取れません。')
        rows=list(csv.reader(io.StringIO(content)))
    elif lower.endswith('.xlsx'):
        ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                if len(z.namelist())>1500 or sum(i.file_size for i in z.infolist())>20_000_000: raise ValueError('Excelファイルが大きすぎます。')
                shared=[]
                if 'xl/sharedStrings.xml' in z.namelist():
                    root=ET.fromstring(z.read('xl/sharedStrings.xml'))
                    shared=[''.join(t.text or '' for t in si.findall('.//m:t',ns)) for si in root.findall('m:si',ns)]
                root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
                rows=[]
                for r in root.findall('.//m:sheetData/m:row',ns):
                    values={}
                    for c in r.findall('m:c',ns):
                        ref=c.get('r',''); m=re.match(r'[A-Z]+',ref)
                        if not m:continue
                        col=0
                        for letter in m.group():col=col*26+ord(letter)-64
                        if col>30:continue
                        v=c.find('m:v',ns)
                        if c.get('t')=='inlineStr': value=''.join(x.text or '' for x in c.findall('.//m:t',ns))
                        elif v is None:value=''
                        elif c.get('t')=='s':value=shared[int(v.text)]
                        else:value=v.text or ''
                        values[col-1]=value
                    rows.append([values.get(i,'') for i in range(max(values.keys(),default=-1)+1)])
        except (zipfile.BadZipFile,KeyError,ET.ParseError,IndexError,TypeError) as e: raise ValueError('Excelファイルを読み取れません。1枚目のシートを確認してください。') from e
    else: raise ValueError('対応形式は .xlsx または .csv です。')
    rows=[r for r in rows if any(str(v).strip() for v in r)]
    if not rows:raise ValueError('ファイルにデータがありません。')
    headers=[str(x).strip().lstrip('\ufeff') for x in rows[0]]
    aliases={'ユーザー名':'username','ログインID':'username','username':'username','表示名':'display_name','氏名':'display_name','display_name':'display_name','パスワード':'password','password':'password','スキル':'skills','対応スキル':'skills','skills':'skills','勤務開始':'shift_start','shift_start':'shift_start','勤務終了':'shift_end','shift_end':'shift_end'}
    mapped=[aliases.get(h) for h in headers]
    if len(set(x for x in mapped if x))!=len([x for x in mapped if x]):raise ValueError('重複する列名があります。')
    required={'username','display_name','password','skills','shift_start','shift_end'}
    if not required.issubset(set(mapped)):raise ValueError('必須列：ユーザー名、表示名、パスワード、スキル、勤務開始、勤務終了')
    if len(rows)-1>300:raise ValueError('一度に登録できるのは300人までです。')
    employees=[]; errors=[]; seen=set()
    for idx,row in enumerate(rows[1:],start=2):
        record={key:str(row[i]).strip() if i<len(row) else '' for i,key in enumerate(mapped) if key}
        username=record['username']; name=record['display_name']; password=record['password']
        skilltext=record['skills'].replace('、',',').replace('／',',').replace('/',',').replace(';',',')
        skills=[x.strip() for x in skilltext.split(',') if x.strip()]
        start=record['shift_start']; end=record['shift_end']
        if not username or not name or len(username)>100 or len(name)>100: errors.append(f'{idx}行目：ユーザー名・表示名を確認してください。')
        if username in seen:errors.append(f'{idx}行目：ファイル内でユーザー名が重複しています。')
        seen.add(username)
        if not valid_password(password):errors.append(f'{idx}行目：パスワードの条件を満たしていません。')
        if not skills or len(skills)!=len(set(skills)) or any(x not in SKILLS for x in skills):errors.append(f'{idx}行目：スキルは Consumer, Merchant, PP をカンマ区切りで指定してください。')
        if not valid_time(start) or not valid_time(end) or (valid_time(start) and valid_time(end) and mins(start)>=mins(end)):errors.append(f'{idx}行目：勤務時刻を10分刻みで正しく指定してください。')
        employees.append((username,name,password,skills,start,end))
    if not employees:raise ValueError('登録対象がありません。')
    if errors:raise ValueError('\n'.join(errors[:15]))
    return employees

class Handler(BaseHTTPRequestHandler):
    server_version='CallCenterLocal/1.0'
    def log_message(self, fmt, *args):
        # Keep logs concise and avoid leaking request bodies or credentials.
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt%args))
    def send_json(self, data, status=200):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>100_000_000: raise ValueError('request too large')
        return json.loads(self.rfile.read(n).decode() or '{}')
    def current_user(self):
        token=self.headers.get('X-Session-Token') or parse_qs(urlparse(self.path).query).get('token',[''])[0]
        sess=SESSIONS.get(token)
        if not sess or sess['expires']<time.time(): return None
        con=db(); row=con.execute('SELECT * FROM users WHERE id=? AND active=1',(sess['user_id'],)).fetchone(); con.close()
        return as_user(row) if row else None
    def require(self, admin=False):
        user=self.current_user()
        if not user: self.send_json({'error':'ログインしてください。'},401); return None
        if admin and user['role']!='admin': self.send_json({'error':'管理者権限が必要です。'},403); return None
        return user
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/' or path=='/index.html':
            f=open(os.path.join(RESOURCE_DIR,'static','index.html'),'rb').read(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(f))); self.end_headers(); self.wfile.write(f); return
        if path=='/api/health': self.send_json({'ok':True,'app':'Call Center Local','version':'0.1.0'}); return
        if path=='/api/me':
            u=self.require();
            if u: self.send_json({'user':u})
            return
        if path=='/api/users':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=con.execute('SELECT * FROM users ORDER BY role DESC,display_name').fetchall(); con.close(); self.send_json({'users':[as_user(r) for r in rows]}); return
        if path=='/api/attendance':
            u=self.require(admin=True)
            if not u:return
            date=parse_qs(urlparse(self.path).query).get('date',[''])[0]
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            con=db()
            rows=con.execute("SELECT id,username,display_name,shift_start,shift_end,skills,active FROM users WHERE role='operator' AND active=1 ORDER BY display_name,id").fetchall()
            saved={r['user_id']:bool(r['present']) for r in con.execute('SELECT user_id,present FROM attendance WHERE attendance_date=?',(date,))}
            con.close()
            self.send_json({'date':date,'operators':[{'id':r['id'],'username':r['username'],'display_name':r['display_name'],'shift_start':r['shift_start'],'shift_end':r['shift_end'],'skills':json.loads(r['skills']),'present':saved.get(r['id'],True)} for r in rows]}); return
        if path=='/api/demand':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=[dict(r) for r in con.execute('SELECT * FROM demand ORDER BY start_time,skill')]; con.close(); self.send_json({'demand':rows,'skills':SKILLS}); return
        if path=='/api/schedule/latest':
            u=self.require()
            if not u:return
            con=db(); row=con.execute("SELECT * FROM schedule_runs WHERE status='confirmed' ORDER BY id DESC LIMIT 1" if u['role']!='admin' else 'SELECT * FROM schedule_runs ORDER BY id DESC LIMIT 1').fetchone(); con.close()
            if not row:self.send_json({'run':None}); return
            result=json.loads(row['result_json'])
            if u['role']!='admin':
                result['schedule']=[x for x in result.get('schedule',[]) if x['operator_id']==u['id']]
                result['breaks']={str(u['id']):result.get('breaks',{}).get(str(u['id']),[])}
            self.send_json({'run':{'id':row['id'],'date':row['schedule_date'],'status':row['status'],'result':result}}); return
        if path=='/api/audit':
            u=self.require(admin=True)
            if not u:return
            con=db(); rows=[dict(r) for r in con.execute('SELECT a.id,a.action,a.details,a.created_at,u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100')]; con.close(); self.send_json({'logs':rows}); return
        if path=='/api/events':
            u=self.require()
            if not u:return
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Cache-Control','no-cache'); self.send_header('Connection','keep-alive'); self.end_headers()
            last=time.time(); end=time.time()+20
            try:
                while time.time()<end:
                    with EVENT_LOCK: fresh=[e for e in EVENTS if e['time']>last]
                    if fresh:
                        for e in fresh: self.wfile.write(('data: '+json.dumps(e)+'\n\n').encode())
                        self.wfile.flush(); last=max(e['time'] for e in fresh)
                    else:
                        self.wfile.write(b': ping\n\n'); self.wfile.flush()
                    time.sleep(1)
            except (BrokenPipeError,ConnectionResetError): pass
            return
        self.send_json({'error':'not found'},404)
    def do_POST(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception as e: self.send_json({'error':'不正なリクエストです。'},400); return
        if path=='/api/login':
            username=str(data.get('username',''))[:100]; password=str(data.get('password',''))
            con=db(); row=con.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone(); con.close()
            if not row or not verify_password(password,row['salt'],row['password_hash']): self.send_json({'error':'ユーザー名またはパスワードが違います。'},401); return
            token=secrets.token_urlsafe(32); SESSIONS[token]={'user_id':row['id'],'expires':time.time()+12*3600}
            audit(row['id'],'login'); self.send_json({'token':token,'user':as_user(row)}); return
        if path=='/api/logout':
            token=self.headers.get('X-Session-Token',''); user=self.current_user()
            if token in SESSIONS: del SESSIONS[token]
            if user:audit(user['id'],'logout')
            self.send_json({'ok':True}); return
        if path=='/api/password':
            actor=self.require()
            if not actor:return
            old=str(data.get('old_password','')); new=str(data.get('new_password',''))
            if not valid_password(new):
                self.send_json({'error':PASSWORD_RULE_MESSAGE},400); return
            con=db(); row=con.execute('SELECT * FROM users WHERE id=?',(actor['id'],)).fetchone()
            if not row or not verify_password(old,row['salt'],row['password_hash']):
                con.close(); self.send_json({'error':'現在のパスワードが違います。'},403); return
            salt,digest=hash_password(new)
            con.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',(salt,digest,actor['id'])); con.commit(); con.close()
            audit(actor['id'],'password_changed'); emit('users'); self.send_json({'ok':True}); return
        if path=='/api/restore':
            actor=self.require(admin=True)
            if not actor:return
            try:
                raw=base64.b64decode(data.get('backup_base64',''),validate=True)
                if not raw or len(raw)>80_000_000: raise ValueError('invalid backup size')
                with tempfile.NamedTemporaryFile(prefix='restore-',suffix='.sqlite3',dir=DATA_DIR,delete=False) as tmp:
                    tmp.write(raw); tmp_path=tmp.name
                try:
                    source=sqlite3.connect(tmp_path)
                    check=source.execute('PRAGMA integrity_check').fetchone()[0]
                    tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    required={'users','demand','schedule_runs','audit_log'}
                    if check!='ok' or not required.issubset(tables): raise ValueError('invalid database')
                    dest=db()
                    source.backup(dest)
                    dest.execute('CREATE TABLE IF NOT EXISTS attendance(attendance_date TEXT NOT NULL,user_id INTEGER NOT NULL,present INTEGER NOT NULL DEFAULT 1 CHECK(present IN (0,1)),updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_by INTEGER,PRIMARY KEY(attendance_date,user_id),FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(updated_by) REFERENCES users(id))')
                    dest.commit(); dest.close(); source.close()
                finally:
                    try: os.remove(tmp_path)
                    except OSError: pass
            except Exception:
                self.send_json({'error':'バックアップを復元できません。ファイル形式と内容を確認してください。'},400); return
            audit(actor['id'],'database_restored'); emit('restore'); self.send_json({'ok':True}); return
        if path=='/api/backup':
            actor=self.require(admin=True)
            if not actor:return
            import io
            con=db(); target=sqlite3.connect(':memory:')
            con.backup(target); target.commit(); payload=target.serialize(); target.close(); con.close()
            self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition','attachment; filename=callcenter-backup.sqlite3'); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
            audit(actor['id'],'database_backup'); return
        if path=='/api/users/import':
            actor=self.require(admin=True)
            if not actor:return
            try:
                filename=str(data.get('filename',''))
                encoded=str(data.get('file_base64',''))
                if len(encoded)>2_700_000:raise ValueError('ファイルは2MB以下にしてください。')
                payload=base64.b64decode(encoded,validate=True)
                employees=parse_employee_file(filename,payload)
                con=db()
                try:
                    con.execute('BEGIN IMMEDIATE')
                    names=[u[0] for u in employees]
                    existing={r['username'] for r in con.execute('SELECT username FROM users WHERE username IN ('+','.join('?' for _ in names)+')',names)}
                    if existing:raise ValueError('登録済みユーザー名：'+', '.join(sorted(existing)[:10]))
                    for username,name,password,skills,start,end in employees:
                        salt,digest=hash_password(password)
                        con.execute('INSERT INTO users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES(?,?,?,?,?,?,?,?)',(username,name,'operator',salt,digest,json.dumps(skills),start,end))
                    con.commit()
                except Exception:
                    con.rollback();raise
                finally:con.close()
                audit(actor['id'],'operators_imported',{'count':len(employees)})
                emit('users');self.send_json({'ok':True,'created':len(employees)},201)
            except (ValueError,sqlite3.IntegrityError,base64.binascii.Error) as e:self.send_json({'error':str(e)},400)
            return
        if path=='/api/users':
            actor=self.require(admin=True)
            if not actor:return
            username=str(data.get('username','')).strip(); name=str(data.get('display_name','')).strip(); role=data.get('role','operator')
            skills=data.get('skills',[]); start=data.get('shift_start','09:00'); end=data.get('shift_end','18:00'); password=str(data.get('password',''))
            if not username or not name or role not in ('admin','operator') or not valid_password(password) or not isinstance(skills,list) or any(s not in SKILLS for s in skills) or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end): self.send_json({'error':('入力を確認してください。' if valid_password(password) else PASSWORD_RULE_MESSAGE)},400); return
            salt,digest=hash_password(password)
            try:
                con=db(); cur=con.execute('INSERT INTO users(username,display_name,role,salt,password_hash,skills,shift_start,shift_end) VALUES(?,?,?,?,?,?,?,?)',(username,name,role,salt,digest,json.dumps(skills),start,end)); con.commit(); uid=cur.lastrowid; con.close()
            except sqlite3.IntegrityError: self.send_json({'error':'このユーザー名は既に使われています。'},409); return
            audit(actor['id'],'user_created',{'user_id':uid}); emit('users'); self.send_json({'ok':True,'id':uid},201); return
        if path=='/api/demand':
            actor=self.require(admin=True)
            if not actor:return
            skill=data.get('skill'); start=data.get('start_time'); end=data.get('end_time'); count=data.get('min_staff')
            try:
                if skill not in SKILLS or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or int(count)<0 or int(count)>200: raise ValueError()
                con=db(); con.execute('INSERT INTO demand(skill,start_time,end_time,min_staff) VALUES(?,?,?,?) ON CONFLICT(skill,start_time,end_time) DO UPDATE SET min_staff=excluded.min_staff',(skill,start,end,int(count))); con.commit(); con.close()
            except Exception: self.send_json({'error':'スキル・時間帯・人数を確認してください。'},400); return
            audit(actor['id'],'demand_updated',data); emit('demand'); self.send_json({'ok':True}); return
        if path=='/api/attendance':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',''))
            entries=data.get('attendance')
            try:
                datetime.strptime(date,'%Y-%m-%d')
                if not isinstance(entries,list): raise ValueError()
                con=db()
                active_ids={r['id'] for r in con.execute("SELECT id FROM users WHERE role='operator' AND active=1")}
                submitted=set()
                for item in entries:
                    uid=int(item['user_id'])
                    if uid not in active_ids or uid in submitted or not isinstance(item.get('present'),bool): raise ValueError()
                    submitted.add(uid)
                if submitted != active_ids: raise ValueError()
                con.execute('BEGIN IMMEDIATE')
                for item in entries:
                    con.execute('INSERT INTO attendance(attendance_date,user_id,present,updated_at,updated_by) VALUES(?,?,?,?,?) ON CONFLICT(attendance_date,user_id) DO UPDATE SET present=excluded.present,updated_at=excluded.updated_at,updated_by=excluded.updated_by', (date,int(item['user_id']),1 if item['present'] else 0,datetime.now().isoformat(timespec='seconds'),actor['id']))
                con.commit(); con.close()
            except Exception:
                try: con.close()
                except Exception: pass
                self.send_json({'error':'出勤状況を保存できません。日付と全オペレーターのチェック状態を確認してください。'},400); return
            audit(actor['id'],'attendance_updated',{'date':date,'present_count':sum(1 for x in entries if x['present']),'total':len(entries)}); emit('attendance'); self.send_json({'ok':True,'date':date}); return
        if path=='/api/schedule/generate':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',datetime.now().date().isoformat()))
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            result=generate_schedule(date,actor['id']); self.send_json(result,200 if result.get('ok') else 422); return
        if path=='/api/schedule/manual':
            actor=self.require(admin=True)
            if not actor:return
            date=str(data.get('date',''))
            try: datetime.strptime(date,'%Y-%m-%d')
            except ValueError: self.send_json({'error':'日付の形式が正しくありません。'},400); return
            result=create_manual_schedule(date,actor['id'],data.get('breaks'))
            self.send_json(result,200 if result.get('ok') else 422); return
        if path=='/api/schedule/confirm':
            actor=self.require(admin=True)
            if not actor:return
            run_id=data.get('run_id'); stage=data.get('override_stage')
            con=db(); row=con.execute("SELECT * FROM schedule_runs WHERE id=? AND status='draft'",(run_id,)).fetchone()
            if not row: con.close(); self.send_json({'error':'確定できる下書きが見つかりません。'},404); return
            result=json.loads(row['result_json']); warnings=result.get('shortages',[])
            if warnings:
                if stage == 1:
                    result['override_stage1_by']=actor['id']; result['override_stage1_at']=datetime.now().isoformat(timespec='seconds')
                    con.execute('UPDATE schedule_runs SET result_json=? WHERE id=?',(json.dumps(result,ensure_ascii=False),run_id)); con.commit(); con.close()
                    audit(actor['id'],'schedule_override_stage1',{'run_id':run_id,'shortage_count':len(warnings)})
                    self.send_json({'ok':True,'needs_second_confirmation':True,'shortages':warnings[:100]}); return
                if stage != 2 or result.get('override_stage1_by') != actor['id']:
                    con.close(); self.send_json({'error':'最低稼働人数不足があります。1段階目の確認を先に完了してください。','needs_first_confirmation':True,'shortages':warnings[:100]},409); return
                result['override_stage2_by']=actor['id']; result['override_stage2_at']=datetime.now().isoformat(timespec='seconds')
                con.execute("UPDATE schedule_runs SET status='confirmed',result_json=? WHERE id=?",(json.dumps(result,ensure_ascii=False),run_id))
                con.commit(); con.close(); audit(actor['id'],'schedule_confirmed_with_staffing_override',{'run_id':run_id,'shortages':warnings[:100]}); emit('schedule')
                self.send_json({'ok':True,'override':True}); return
            con.execute("UPDATE schedule_runs SET status='confirmed' WHERE id=?",(run_id,)); con.commit(); con.close(); audit(actor['id'],'schedule_confirmed',{'run_id':run_id}); emit('schedule'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)
    def do_PUT(self):
        path=urlparse(self.path).path
        try: data=self.body()
        except Exception: self.send_json({'error':'不正なリクエストです。'},400); return
        if path.startswith('/api/users/') and path.endswith('/reset-password'):
            actor=self.require(admin=True)
            if not actor: return
            try: uid=int(path.split('/')[3])
            except (ValueError, IndexError): self.send_json({'error':'ユーザーIDが不正です。'},400); return
            password=data.get('new_password')
            if not valid_password(password): self.send_json({'error':PASSWORD_RULE_MESSAGE},400); return
            con=db(); row=con.execute('SELECT id,role FROM users WHERE id=? AND active=1',(uid,)).fetchone()
            if not row or row['role']!='operator':
                con.close(); self.send_json({'error':'有効なオペレーターが見つかりません。'},404); return
            salt,digest=hash_password(password)
            con.execute('UPDATE users SET salt=?,password_hash=? WHERE id=?',(salt,digest,uid)); con.commit(); con.close()
            for key, session in list(SESSIONS.items()):
                if session['user_id']==uid: SESSIONS.pop(key,None)
            audit(actor['id'],'operator_password_reset',{'user_id':uid}); emit('users')
            self.send_json({'ok':True}); return
        if path.startswith('/api/users/'):
            actor=self.require(admin=True)
            if not actor:return
            try: uid=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'ユーザーIDが不正です。'},400); return
            con=db(); row=con.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not row: con.close(); self.send_json({'error':'ユーザーが見つかりません。'},404); return
            if set(data.keys())=={'active'}:
                if uid==actor['id'] and not bool(data['active']): con.close(); self.send_json({'error':'自分自身を無効化できません。'},400); return
                con.execute('UPDATE users SET active=? WHERE id=?',(1 if data['active'] else 0,uid)); con.commit(); con.close()
                audit(actor['id'],'user_activated' if data['active'] else 'user_deactivated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
            username=str(data.get('username','')).strip(); name=str(data.get('display_name','')).strip(); skills=data.get('skills',[])
            start=data.get('shift_start','09:00'); end=data.get('shift_end','18:00'); password=str(data.get('password',''))
            if not username or not name or not isinstance(skills,list) or any(x not in SKILLS for x in skills) or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or (password and not valid_password(password)):
                con.close(); self.send_json({'error':(PASSWORD_RULE_MESSAGE if password and not valid_password(password) else '入力を確認してください。勤務時間は10分刻み、終了は開始より後です。')},400); return
            try:
                if password:
                    salt,digest=hash_password(password)
                    con.execute('UPDATE users SET username=?,display_name=?,skills=?,shift_start=?,shift_end=?,salt=?,password_hash=? WHERE id=?',(username,name,json.dumps(skills),start,end,salt,digest,uid))
                else:
                    con.execute('UPDATE users SET username=?,display_name=?,skills=?,shift_start=?,shift_end=? WHERE id=?',(username,name,json.dumps(skills),start,end,uid))
                con.commit(); con.close()
            except sqlite3.IntegrityError:
                con.close(); self.send_json({'error':'このユーザー名は既に使われています。'},409); return
            audit(actor['id'],'user_updated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
        if path.startswith('/api/demand/'):
            actor=self.require(admin=True)
            if not actor:return
            try: did=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'設定IDが不正です。'},400); return
            skill=data.get('skill'); start=data.get('start_time'); end=data.get('end_time'); count=data.get('min_staff')
            try:
                if skill not in SKILLS or not valid_time(start) or not valid_time(end) or mins(start)>=mins(end) or int(count)<0 or int(count)>200: raise ValueError()
                con=db(); exists=con.execute('SELECT id FROM demand WHERE id=?',(did,)).fetchone()
                if not exists: con.close(); self.send_json({'error':'設定が見つかりません。'},404); return
                con.execute('UPDATE demand SET skill=?,start_time=?,end_time=?,min_staff=? WHERE id=?',(skill,start,end,int(count),did)); con.commit(); con.close()
            except sqlite3.IntegrityError:
                self.send_json({'error':'同じスキル・開始・終了の設定が既にあります。'},409); return
            except Exception:
                self.send_json({'error':'スキル・時間帯・人数を確認してください。'},400); return
            audit(actor['id'],'demand_updated',data); emit('demand'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)

    def do_DELETE(self):
        path=urlparse(self.path).path
        if path.startswith('/api/users/'):
            actor=self.require(admin=True)
            if not actor:return
            try: uid=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'invalid id'},400); return
            if uid==actor['id']: self.send_json({'error':'自分自身は無効化できません。'},400); return
            con=db(); con.execute("UPDATE users SET active=0 WHERE id=? AND role='operator'",(uid,)); changed=con.total_changes; con.commit(); con.close()
            if not changed: self.send_json({'error':'対象のオペレーターが見つかりません。'},404); return
            audit(actor['id'],'user_deactivated',{'user_id':uid}); emit('users'); self.send_json({'ok':True}); return
        if path.startswith('/api/demand/'):
            actor=self.require(admin=True)
            if not actor:return
            try: did=int(path.rsplit('/',1)[1])
            except ValueError: self.send_json({'error':'invalid id'},400); return
            con=db(); cur=con.execute('DELETE FROM demand WHERE id=?',(did,)); con.commit(); con.close()
            if not cur.rowcount: self.send_json({'error':'設定が見つかりません。'},404); return
            audit(actor['id'],'demand_deleted',{'demand_id':did}); emit('demand'); self.send_json({'ok':True}); return
        self.send_json({'error':'not found'},404)

def main():
    init_db()
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    server.daemon_threads=True
    print('='*62)
    print('コールセンター稼働最適化アプリ（ローカルLAN版）')
    print(f'管理者PC内の確認用URL: http://127.0.0.1:{PORT}')
    computer_name = socket.gethostname()
    print(f'社内LAN向けURL候補（PC名での名前解決が有効な場合）: http://{computer_name}:{PORT}/')
    print('この画面にはLAN内のIPアドレスを表示しません。')
    print('初期アカウント情報はREADME.mdの「初期アカウント」を確認し、初回ログイン後すぐに変更してください。')
    print('停止するには Ctrl+C を押してください。')
    print('='*62)
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nサーバーを停止します。')
    finally: server.server_close()

if __name__=='__main__': main()
