"""10-minute draft planning: demand-aware shift choices, skill switching and breaks.

Pure standard-library implementation; this is an auditable heuristic, not a mathematical
optimality guarantee. All unfilled requirements are reported, never concealed.
Times are same-day HH:MM (09:20-23:00) and end times are exclusive.
An operator is registered on EVERY eligible call queue while working; the
preferred/primary skill is only a routing preference, not an exclusive queue.
Queue-ready counts intentionally overlap; distinct simultaneous handling
capacity is validated separately with a one-person-one-call matching model.
"""
from collections import Counter
from functools import lru_cache
from datetime import datetime

SKILLS = ('consumer', 'merchant', 'PP')
STEP = 10
OPEN = 9*60+20
CLOSE = 23*60
SLOTS = tuple(range(OPEN, CLOSE, STEP))

def minute(t):
    h, m = (int(x) for x in t.split(':'))
    return h*60+m

def clock(t):
    return f'{t//60:02d}:{t%60:02d}'

@lru_cache(maxsize=512)
def _skills_from_text(value):
    found=set()
    for item in value.replace('、', ',').replace(';', ',').replace('/', ',').split(','):
        name=item.strip().lower()
        if name=='pp':found.add('PP')
        elif name in ('consumer','merchant'):found.add(name)
    return tuple(s for s in SKILLS if s in found)


def skills_of(value):
    if isinstance(value,(list,tuple,set)):
        return _skills_from_text(','.join(map(str,value)))
    return _skills_from_text(str(value or ''))

def demands_for_day(rules, weekday):
    """Maximum overlapping rule, per skill and 10-minute slot."""
    req = {slot: {skill:0 for skill in SKILLS} for slot in SLOTS}
    for r in rules:
        if int(r['weekday']) != weekday: continue
        skill = skills_of([r['skill']])
        if not skill: continue
        sk = skill[0]
        start, end = minute(r['start_time']), minute(r['end_time'])
        for slot in SLOTS:
            if start <= slot < end:
                req[slot][sk] = max(req[slot][sk], int(r['min_count']))
    return req

def _available(people, slot, occupied):
    return [person for person in people if minute(person['start_time']) <= slot < minute(person['end_time'])
            and slot not in occupied.get(person['employee_id'], ()) and skills_of(person['skills'])]

def simultaneous_queue_coverage(people, slot, required, occupied=None):
    """Count queue-ready people for every skill, even when qualifications overlap.

    Each available worker contributes to ALL qualified queues (call routing
    membership), but is still one physical person. The exclusive assignment
    model estimates independent capacity if every queue receives a call at
    once, and exposes deficits separately rather than inflating headcount.
    """
    available = _available(people, slot, occupied or {})
    queue_ready = {sk:sum(sk in skills_of(p['skills']) for p in available) for sk in SKILLS}
    # Retain the original conservative maximum matching calculation as the
    # distinct simultaneous-call capacity check.
    _preferred, deficits = match_slot(people, slot, required, occupied)
    unique = len(available)
    return queue_ready, deficits, unique

MAX_AUTO_BLOCK_SLOTS = 6  # 60 minutes, with 10-minute units
MAX_AUTO_TOTAL_SLOTS = 12  # 120 minutes total per person/day
MAX_AUTO_EVENTS = 2
AUTO_COOLDOWN_SLOTS = 3  # 30 minutes before another temporary assignment


def choose_primary_skills(people, req):
    """Assign exactly one BASE skill per operator for the whole day.

    Existing primary_skill/skill values are pinned; only unassigned operators
    are optimized. This prevents a monthly regeneration from changing finalized
    or manually selected primary duties. All scores count person-timeslot at
    most once and do not assume incidental switching.
    """
    people = [dict(p) for p in people]
    assigned = {}
    candidates = []
    for p in people:
        uid = p['employee_id']
        opts = skills_of(p.get('skills'))
        if not opts:
            continue
        pinned = p.get('primary_skill') or p.get('skill')
        if pinned in opts:
            assigned[uid] = pinned
        else:
            candidates.append(p)
    # Scarce/single-skill operators first, then those with longer overlap.
    candidates.sort(key=lambda p: (len(skills_of(p['skills'])),
        -(minute(p['end_time']) - minute(p['start_time'])), p['employee_id']))
    present = {p['employee_id']:tuple(slot for slot in SLOTS if
        minute(p['start_time']) <= slot < minute(p['end_time'])) for p in people}
    counts = {t:Counter() for t in SLOTS}
    for uid, skill in assigned.items():
        for t in present[uid]: counts[t][skill] += 1
    for p in candidates:
        uid=p['employee_id']
        opts=skills_of(p['skills'])
        def benefit(skill):
            return sum(1 for t in present[uid] if counts[t][skill] < req[t][skill])
        best=max(opts, key=lambda skill:(benefit(skill),
                      -sum(counts[t][skill] for t in present[uid]), -SKILLS.index(skill)))
        assigned[uid]=best
        for t in present[uid]: counts[t][best]+=1
    # Coordinate descent improves the all-day staffing balance without
    # changing a pinned assignment. A change must reduce the total shortage.
    for _ in range(2):
        changed=False
        for p in candidates:
            uid=p['employee_id']; old=assigned[uid]
            active=present[uid]
            def total_loss(skill):
                return sum(max(0, req[t][sk] - (counts[t][sk] - (sk==old) + (sk==skill)))
                           for t in active for sk in SKILLS)
            best=min(skills_of(p['skills']), key=lambda sk:(total_loss(sk),sk!=old,SKILLS.index(sk)))
            if best!=old:
                for t in active:
                    counts[t][old]-=1
                    counts[t][best]+=1
                assigned[uid]=best;changed=True
        if not changed:break
    return assigned


def match_slot(people, slot, required, occupied=None, previous=None, forced=None):
    """Maximize simultaneous skill coverage using ALL registered abilities.

    Primary duty is a stable starting preference, never an eligibility limit.
    Improvements are accepted only when they reduce an actual shortage; this
    avoids changing a person's role merely to move a shortage elsewhere.
    Reassigning a chain of multi-skilled people may free the scarce specialist
    needed for a different skill. Each worker has exactly one duty per slot.
    """
    available = _available(people, slot, occupied or {})
    by_id = {p['employee_id']:p for p in available}
    previous = previous or {}
    forced = forced or {}
    chosen = {}
    pinned = set()
    for uid,p in by_id.items():
        opts=skills_of(p['skills'])
        override=forced.get(uid)
        if override in opts:
            chosen[uid]=override
            pinned.add(uid)
        else:
            preferred=previous.get(uid) or p.get('primary_skill') or p.get('skill')
            chosen[uid]=preferred if preferred in opts else opts[0]
    counts=Counter(chosen.values())
    need={sk:max(0,int(required.get(sk,0))) for sk in SKILLS}

    # Where a previous temporary duty has become unnecessary, return to the
    # primary skill if this does not introduce a shortage in the previous duty.
    for uid,p in by_id.items():
        primary=p.get('primary_skill')
        old=chosen[uid]
        if (uid not in pinned and primary in skills_of(p['skills']) and old!=primary
                and counts[old]>need[old]):
            chosen[uid]=primary
            counts[old]-=1;counts[primary]+=1

    def fill_gap(wanted, locked, path):
        # Resolve one shortage using somebody from a surplus role, possibly
        # through a chain of intermediate multi-skilled reassignments.
        candidates = [uid for uid,p in by_id.items()
                      if uid not in pinned and uid not in locked
                      and chosen[uid]!=wanted and wanted in skills_of(p['skills'])
                      and chosen[uid] not in path]
        candidates.sort(key=lambda uid:(counts[chosen[uid]]<=need[chosen[uid]],
                         previous.get(uid)!=wanted,by_id[uid].get('primary_skill')!=wanted,
                         len(skills_of(by_id[uid]['skills'])),uid))
        for uid in candidates:
            old=chosen[uid]
            chosen[uid]=wanted
            counts[old]-=1;counts[wanted]+=1
            # Moving out of a non-surplus skill is permitted only if someone
            # else can cover the vacated seat before accepting the change.
            if counts[old]>=need[old] or fill_gap(old,locked | {uid},path | {wanted}):
                return True
            chosen[uid]=old
            counts[old]+=1;counts[wanted]-=1
        return False

    # Skills with fewer eligible staff get the first opportunity for coverage.
    order=sorted(SKILLS,key=lambda sk:(sum(sk in skills_of(p['skills']) for p in by_id.values()),
                                       -need[sk],SKILLS.index(sk)))
    for sk in order:
        while counts[sk]<need[sk]:
            if not fill_gap(sk,set(),{sk}):break
    return chosen,{sk:max(0,need[sk]-counts[sk]) for sk in SKILLS}


def plan_temporary_switches(people, req, occupied, manual_forced):
    """Plan flexible, break-aware skill coverage with no artificial switch cap.

    Daily primary remains the default; a multi-skilled employee is ALWAYS
    eligible for every registered skill. Switch when it can reduce shortages,
    favor keeping the current role otherwise, and never double-count someone.
    """
    decisions = {}
    switched_slots = {}
    previous = {}
    for slot in SLOTS:
        chosen, unused = match_slot(people, slot, req[slot], occupied,
                                    previous=previous, forced=manual_forced.get(slot, {}))
        decisions[slot] = chosen
        by_id = {p['employee_id']:p for p in people}
        for uid, skill in chosen.items():
            if (uid not in manual_forced.get(slot,{}) and
                skill != by_id[uid].get('primary_skill')):
                switched_slots[(uid,slot)] = skill
        previous = chosen
    return decisions, switched_slots

def _break_specs(person):
    """Choose mandatory breaks from scheduled (start-to-end) shift duration.

    Shifts up to 6 hours: paid 10-minute breaks only (one when shorter
    than 6 hours, two at exactly 6 hours). Shifts LONGER than 6 hours:
    60-minute unpaid lunch plus two paid 10-minute breaks. This overrides
    any older shift-pattern break_minutes setting.
    """
    length = minute(person['end_time']) - minute(person['start_time'])
    if length <= 0:
        return []
    specs = []
    if length > 6 * 60:
        specs.append(('昼休憩', 60, 'unpaid'))
    specs.append(('前半10分休憩', STEP, 'paid'))
    if length >= 6 * 60:
        specs.append(('後半10分休憩', STEP, 'paid'))
    return specs


# Lunch times indicate START, on a ten-minute grid. Full-day shifts use
# the same offset from the actual start as the standard early shift:
# 09:20 -> 11:30..14:00 (130..280 minutes after clock-in).
# Other shifts follow the same relative 150-minute wide window, constrained
# by the absolute lunch start limits and the shift end.
LUNCH_FIRST_START = 11*60 + 30
LUNCH_LAST_START = 18*60 + 50
EARLY_LUNCH_EARLIEST = 11*60 + 30
EARLY_LUNCH_LATEST = 14*60
LUNCH_AFTER_SHIFT_MIN = 2*60+10
LUNCH_AFTER_SHIFT_MAX = 4*60+40
BREAK_GAP = 20  # minimum time between end of one break and next


def lunch_window(person):
    """Base time range, before end-of-shift feasibility is applied."""
    start = minute(person['start_time'])
    if shift_kind(person) == '早番':
        return EARLY_LUNCH_EARLIEST, EARLY_LUNCH_LATEST
    return (max(LUNCH_FIRST_START, start + LUNCH_AFTER_SHIFT_MIN),
            min(LUNCH_LAST_START, start + LUNCH_AFTER_SHIFT_MAX))


def shift_kind(person):
    """Use the registered shift category when available, otherwise work start.

    Supports older databases/patterns whose shift_type is 'auto'. When the
    shift type is unknown, a 10:30 or 13:00 start separates the usual shifts.
    """
    specified = str(person.get('shift_type') or '').strip()
    aliases = {'early':'早番', 'middle':'中番', 'mid':'中番', 'late':'遅番'}
    specified = aliases.get(specified.lower(), specified)
    if specified in ('早番', '中番', '遅番'):
        return specified
    label = str(person.get('pattern_name') or person.get('name_of_pattern') or '')
    for name in ('早番', '中番', '遅番'):
        if name in label:
            return name
    start = minute(person['start_time'])
    return '早番' if start < 10*60+30 else '中番' if start < 13*60 else '遅番'


def _aligned_range(lower, upper):
    first = ((lower + STEP-1)//STEP)*STEP
    last = (upper//STEP)*STEP
    return list(range(first, last+1, STEP)) if first <= last else []


def lunch_candidates(person, duration=60):
    """Full-day lunch start candidates (inclusive endpoints, 10-minute grid).

    The standard early shift 09:20 starts lunch from 11:30 through 14:00.
    Later-starting middle/late shifts use the same offsets from their clock-in,
    with the universal 11:30..18:50 restriction. The lunch must fit in the
    shift and leave at least twenty minutes of time afterward.
    """
    start, end = minute(person['start_time']), minute(person['end_time'])
    if not any(name == '昼休憩' for name,_,_ in _break_specs(person)):
        return []
    if duration <= 0 or duration % STEP or end-start < duration + 80:
        return []
    low, high = lunch_window(person)
    return _aligned_range(max(LUNCH_FIRST_START, low, start+60),
                          min(LUNCH_LAST_START, high, end-duration-20))


def lunch_first_allowed(person, lunch_start):
    """Lunch-first exemption: late shifts, or early only at precisely 11:30."""
    return (shift_kind(person) == '遅番' or
            (shift_kind(person) == '早番' and lunch_start == LUNCH_FIRST_START))


def _break_candidates(person, name, duration, lunch_start=None, first_start=None):
    """Candidate break starts; optional lunch/first break pins order.

    The late shift may always take lunch before the first 10-minute break.
    The early shift MUST take its first paid break after lunch when lunch
    starts at 11:30. At all other early lunch times it must be before lunch.
    The middle shift must take the first 10-minute break before lunch.
    When a shift has no lunch, only the paid 10-minute breaks are scheduled.
    """
    start, end = minute(person['start_time']), minute(person['end_time'])
    if end-start <= duration or duration <= 0: return []
    if name == '昼休憩':
        return lunch_candidates(person, duration)
    lower, upper = start+60, end-duration-50
    if lower>upper:return []
    if lunch_start is None:
        lunches = lunch_candidates(person, 60)
        if lunches:
            vals=set()
            for meal in lunches:
                vals.update(_break_candidates(person,name,duration,meal,first_start))
            return sorted(vals)
        # Only schedules of six hours or less have no lunch.
        if name == '後半10分休憩' and first_start is not None:
            lower=max(lower, first_start+STEP+BREAK_GAP)
        return _aligned_range(lower,upper)
    lunch_duration = 60
    if name == '前半10分休憩':
        before = _aligned_range(lower, min(upper,lunch_start-duration-BREAK_GAP))
        if not lunch_first_allowed(person, lunch_start):
            return before
        # Preserve the existing late-shift choice, but make lunch first
        # MANDATORY for early shifts whose lunch begins precisely at 11:30.
        after = _aligned_range(max(lower,lunch_start+lunch_duration+BREAK_GAP),
                               min(upper,end-2*duration-2*BREAK_GAP-50))
        if shift_kind(person) == '早番' and lunch_start == LUNCH_FIRST_START:
            return after
        return sorted(set(before+after))
    if name == '後半10分休憩':
        lower=max(lower,lunch_start+lunch_duration+BREAK_GAP)
        if first_start is not None:
            lower=max(lower,first_start+STEP+BREAK_GAP)
        return _aligned_range(lower,upper)
    return _aligned_range(lower,upper)


def valid_break_sequence(person, assigned):
    """Validate an operator's complete manual/automatic break ordering.

    assigned maps morning/lunch/afternoon to minute starts. The two paid
    10-minute breaks must not overlap other breaks or each other, and must
    occur in their named order. Early lunch-first is REQUIRED at 11:30,
    forbidden at all other early lunch starts, and optional for late shifts.
    """
    durations={('morning' if name=='前半10分休憩' else
                'afternoon' if name=='後半10分休憩' else 'lunch'): dur
               for name,dur,_ in _break_specs(person)}
    for key,value in assigned.items():
        if key not in durations:return False
        if value not in _break_candidates(person,
                  {'morning':'前半10分休憩','lunch':'昼休憩','afternoon':'後半10分休憩'}[key],
                  durations[key]):return False
    if not assigned or 'lunch' not in assigned:
        if 'morning' in assigned and 'afternoon' in assigned:
            return assigned['morning']+STEP+BREAK_GAP<=assigned['afternoon']
        return True
    meal=assigned['lunch']; mend=meal+durations['lunch']
    first=assigned.get('morning'); second=assigned.get('afternoon')
    if second is not None and second<mend+BREAK_GAP:return False
    if first is not None:
        before=first+STEP+BREAK_GAP<=meal
        after=first>=mend+BREAK_GAP and lunch_first_allowed(person, meal)
        # At 11:30 for the early shift, the morning 10-minute break MUST
        # begin at least BREAK_GAP minutes after the 60-minute lunch ends.
        if shift_kind(person) == '早番' and meal == LUNCH_FIRST_START:
            if not after:return False
        elif not (before or after):return False
        if second is not None and first+STEP+BREAK_GAP>second:return False
    return True

def schedule_breaks(people, req, manual=None):
    """Place breaks while re-optimizing all eligible skills at each time.

    An operator absent on break cannot be allocated to ANY skill. Candidate
    breaks are scored on the maximum achievable staffing after that absence,
    not merely the employee's preferred primary skill. Manual overrides are
    respected where possible; unmet minima remain visible in the final plan.
    """
    manual=manual or []
    override_slots={p['employee_id']:set() for p in people}
    manual_by_slot={slot:{} for slot in SLOTS}
    for override in manual:
        uid=int(override['employee_id'])
        if uid not in override_slots:continue
        for slot in SLOTS:
            if minute(override['start_time'])<=slot<minute(override['end_time']):
                override_slots[uid].add(slot)
                if override['skill'] in skills_of(next(p['skills'] for p in people if p['employee_id']==uid)):
                    manual_by_slot[slot][uid]=override['skill']
    occupied={p['employee_id']:set() for p in people}
    result={p['employee_id']:[] for p in people}
    issues=[]
    jobs=[]
    for p in people:
        for name,dur,paid in _break_specs(p):
            positions=_break_candidates(p,name,dur)
            # All lunches are assigned before either microbreak, so the 10-min
            # breaks can take the actual lunch time into account.
            phase={'昼休憩':0,'前半10分休憩':1,'後半10分休憩':2}[name]
            jobs.append((phase,len(positions),len(skills_of(p['skills'])),p['employee_id'],p,name,dur,paid))
    jobs.sort(key=lambda x:x[:4])
    for _,_,_,uid,p,name,dur,paid in jobs:
        existing = {('morning' if x['type']=='前半10分休憩' else
                     'afternoon' if x['type']=='後半10分休憩' else 'lunch'):
                    minute(x['start']) for x in result[uid]}
        lunch=existing.get('lunch')
        first=existing.get('morning')
        positions=_break_candidates(p,name,dur,lunch,first)
        best=None
        for start in positions:
            kind=('morning' if name=='前半10分休憩' else
                  'afternoon' if name=='後半10分休憩' else 'lunch')
            proposed=dict(existing,**{kind:start})
            if not valid_break_sequence(p,proposed):continue
            if kind == 'lunch':
                # Lunch is placed first by the optimizer: do not consume a slot
                # that makes the mandatory morning/afternoon breaks impossible.
                morning_choices = _break_candidates(p,'前半10分休憩',STEP,start)
                if not any(valid_break_sequence(p,{'lunch':start,'morning':m,'afternoon':a})
                           for m in morning_choices
                           for a in _break_candidates(p,'後半10分休憩',STEP,start,m)):
                    continue
            if kind=='morning' and any(spec[0]=='後半10分休憩' for spec in _break_specs(p)):
                if not any(valid_break_sequence(p,dict(proposed,afternoon=after))
                           for after in _break_candidates(p,'後半10分休憩',STEP,lunch,start)):
                    continue
            block=tuple(range(start,start+dur,STEP))
            if any(t not in req or t in occupied[uid] for t in block):continue
            score=10000*sum(t in override_slots[uid] for t in block)
            for t in block:
                occupied[uid].add(t)
                queue_ready, physical_gaps, _ = simultaneous_queue_coverage(
                    people, t, req[t], occupied)
                occupied[uid].remove(t)
                score += 10000*sum(max(0,req[t][sk]-queue_ready[sk]) for sk in SKILLS)
                score += 1000*sum(physical_gaps.values())
                score += sum(t in stamps for other,stamps in occupied.items() if other!=uid)
            # Favor staggered lunch starts and a reasonable time between rests
            # as a tiebreaker; staffing deficits ALWAYS outweigh preferences.
            if kind=='lunch':
                low,high=lunch_window(p)
                target=(low+high)//2
            elif kind=='morning':
                target=(lunch+int(p.get('break_minutes') or 60)+90
                        if lunch is not None and lunch_first_allowed(p,lunch) and start>lunch
                        else minute(p['start_time'])+100)
            else:
                target=(minute(p['end_time'])-110)
            score+=abs(start-target)*.001
            candidate=(score,start)
            if best is None or candidate<best:best=candidate
        if best is None:
            issues.append(f"{p['name']}：{name}（{dur}分）を勤務時間内に配置できません")
            continue
        start=best[1]
        occupied[uid].update(range(start,start+dur,STEP))
        result[uid].append({'type':name,'start':clock(start),'end':clock(start+dur),
                            'minutes':dur,'paid':paid=='paid'})
    for rows in result.values():rows.sort(key=lambda x:x['start'])
    return occupied,result,issues


def build_day_plan(work_date, people, rules, manual=None):
    """A stable daily preference with dynamic use of every registered skill."""
    weekday=datetime.strptime(work_date,'%Y-%m-%d').weekday()
    people=[dict(p) for p in people]
    req=demands_for_day(rules,weekday)
    primaries=choose_primary_skills(people,req)
    for p in people:
        p['primary_skill']=primaries.get(p['employee_id'])
    manual=manual or []
    occupied,breaks,issues=schedule_breaks(people,req,manual)
    forced={s:{} for s in SLOTS}
    for override in manual:
        uid=int(override['employee_id']);skill=override['skill']
        if skill not in SKILLS:continue
        for s in SLOTS:
            if minute(override['start_time'])<=s<minute(override['end_time']):
                forced[s][uid]=skill
    assigned,auto_slots=plan_temporary_switches(people,req,occupied,forced)
    details={p['employee_id']:[] for p in people}
    shortages=[];capacity_warnings=[];staffing=[]
    for t in SLOTS:
        chosen=assigned[t]
        queue_ready, physical_gaps, unique = simultaneous_queue_coverage(people,t,req[t],occupied)
        shortage={sk:max(0,req[t][sk]-queue_ready[sk]) for sk in SKILLS}
        physical_gap_count=sum(physical_gaps.values())
        staffing.append({'time':clock(t),'required':req[t],
                         'assigned':queue_ready,'shortage':shortage,
                         'simultaneous_deficit':physical_gap_count,
                         'physical_gap_by_skill':physical_gaps,
                         'distinct_people':unique})
        for sk,gap in shortage.items():
            if gap:shortages.append({'time':clock(t),'skill':sk,'required':req[t][sk],
                                     'assigned':queue_ready[sk],'shortage':gap})
        if physical_gap_count:
            capacity_warnings.append({'time':clock(t),
                'required_total':sum(req[t].values()),'distinct_people':unique,
                'shortage':physical_gap_count,'by_skill':physical_gaps})
        for p in people:
            uid=p['employee_id']
            if minute(p['start_time'])<=t<minute(p['end_time']):
                # One person may be queue-ready for Consumer AND Merchant.
                # Preferred is only the suggested routing priority.
                if t in occupied[uid]:
                    status=('休憩',(),None)
                else:
                    skills=skills_of(p['skills'])
                    status=(' / '.join(skills) if skills else '待機',skills,chosen.get(uid))
                details[uid].append((t,status))
    segments=[]
    for p in people:
        uid=p['employee_id']; rows=details[uid]
        if not rows:continue
        start,old=rows[0];prev=start
        def add_segment(begin,finish,state):
            label,skills,preferred=state
            segments.append({'employee_id':uid,'name':p['name'],
                'start':clock(begin),'end':clock(finish),
                'skill':label,'skills':list(skills),
                'preferred_skill':preferred,
                'temporary':preferred is not None and preferred!=p['primary_skill']})
        for t,state in rows[1:]:
            if state!=old or t!=prev+STEP:
                add_segment(start,prev+STEP,old)
                start,old=t,state
            prev=t
        add_segment(start,prev+STEP,old)
    auto_change_count=sum(1 for seg in segments if seg['temporary'] and
                          any(auto_slots.get((seg['employee_id'],t))==seg['preferred_skill']
                              for t in range(minute(seg['start']),minute(seg['end']),STEP)))
    return {'date':work_date,'slots':len(SLOTS),'segments':segments,
            'breaks':{str(uid):rows for uid,rows in breaks.items()},'staffing':staffing,
            'shortages':shortages,'capacity_warnings':capacity_warnings,
            'issues':issues,'skill_switches':auto_change_count,
            'primary_skills':{str(k):v for k,v in primaries.items()},
            'temporary_minutes':len(auto_slots)*STEP,
            'coverage_met':not bool(shortages or capacity_warnings or issues),
            'queue_minimums_met':not bool(shortages),
            'simultaneous_capacity_met':not bool(capacity_warnings),
            'generated_at':datetime.now().isoformat(timespec='seconds'),
            'people':[{'employee_id':p['employee_id'],'name':p['name'],
                'start':p['start_time'],'end':p['end_time'],
                'primary_skill':p['primary_skill'],
                'skills':list(skills_of(p['skills']))} for p in people],
            'explanation':'複数スキル保持者は勤務中、Consumer・Merchant・PPの登録済み全キューで同時に受電待機します。表示するスキル別対応可能人数には同じ人が重複しますが、1人が同時に複数件の通話を処理できるとはみなしません。別途「同時着信時の実人数不足」を表示します。休憩中は全スキルで受電不可です。主担当・手動指定は優先受電スキルの目安です。'}

def approximate_gain(person, req, current_people):
    """Coverage gain from adding one candidate while honoring multi-skill exclusivity."""
    gain=0.0
    for slot in SLOTS:
        if not (minute(person['start_time'])<=slot<minute(person['end_time'])): continue
        chosen,missing=match_slot(current_people,slot,req[slot])
        possibilities=skills_of(person['skills'])
        opportunities=[missing[s] for s in possibilities]
        if opportunities and max(opportunities)>0:
            gain+=1.0 + 0.1*sum(opportunities)
    # Account for predicted breaks (without pretending 100% on-phone presence).
    total=(minute(person['end_time'])-minute(person['start_time']))/STEP
    if total: gain*=max(0.1,1.0-(int(person.get('break_minutes',0))+20)/STEP/max(1,total))
    return gain


# Fast, deterministic 10-minute pattern rebalance. The candidate options have
# already been checked against individual shift permissions, duration limits,
# and approved half-day leave by the caller. This step never changes workdays.
# It builds one-person-per-slot headcount and multi-queue-ready headcount,
# including an extra reserve to give the subsequent break planner flexibility.
# A full break-aware plan is still used to ACCEPT or REJECT the proposal.
def rebalance_work_patterns(people, options, req, max_changes=35):
 # options map id -> list of p dicts
 people=[dict(x) for x in people]
 demand=[req[t] for t in SLOTS]
 poss={p['employee_id']:skills_of(p['skills']) for p in people}
 counts=[[0]*3 for _ in SLOTS];physical=[0]*len(SLOTS)
 def contribution(p):
  s,e=minute(p['start_time']),minute(p['end_time'])
  return [1 if s<=t<e else 0 for t in SLOTS]
 bits={p['employee_id']:contribution(p) for p in people}
 sidx={s:i for i,s in enumerate(SKILLS)}
 for p in people:
  for j,b in enumerate(bits[p['employee_id']]):
   if b:
    physical[j]+=1
    for sk in poss[p['employee_id']]:counts[j][sidx[sk]]+=1
 def penalty():
  val=0
  for j,r in enumerate(demand):
   tot=sum(r.values())
   if not tot:continue
   for sk in SKILLS:
    need=int(r[sk]);have=counts[j][sidx[sk]]
    if need:
     val+=300*max(0,need-have)**2+9*max(0,need+1-have)**2
   val+=300*max(0,tot-physical[j])**2+5*max(0,tot+2-physical[j])**2
  return val
 curr=penalty();changes=0
 for _ in range(max_changes):
  best=None
  for index,p in enumerate(people):
   if p.get('finalized'):continue
   uid=p['employee_id'];old=bits[uid]
   for opt in options.get(uid,[]):
    if opt['id']==p['pattern_id']:continue
    new=contribution(opt)
    for j in range(len(SLOTS)):
     diff=new[j]-old[j]
     if diff:
      physical[j]+=diff
      for sk in poss[uid]:counts[j][sidx[sk]]+=diff
    cost=penalty()
    if cost < curr and (best is None or (cost,opt['id'],uid)<(best[0],best[3]['id'],best[2]['employee_id'])):
     best=(cost,index,p,opt,new)
    for j in range(len(SLOTS)):
     diff=old[j]-new[j]
     if diff:
      physical[j]+=diff
      for sk in poss[uid]:counts[j][sidx[sk]]+=diff
  if best is None:break
  new_cost,index,p,opt,new=best
  old=bits[p['employee_id']]
  for j in range(len(SLOTS)):
   diff=new[j]-old[j]
   if diff:
    physical[j]+=diff
    for sk in poss[p['employee_id']]:counts[j][sidx[sk]]+=diff
  p.update(pattern_id=opt['id'],start_time=opt['start_time'],end_time=opt['end_time'],break_minutes=opt['break_minutes'])
  bits[p['employee_id']]=new
  curr=new_cost
  changes+=1
 return people,changes,curr

