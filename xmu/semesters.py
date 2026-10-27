from datetime import date, datetime, timedelta, timezone
import re

COUNTS = ('total','present','absent','late','leave','unknown','pending')


def term_parts(label):
    year=re.search(r'(\d{4})\s*[-—–/]\s*(\d{4})',label)
    term=re.search(r'第\s*([123一二三])\s*学期',label)
    if not year or not term:
        return None
    n={'一':1,'二':2,'三':3}.get(term.group(1))
    return int(year.group(1)), n or int(term.group(1))


def group_courses(courses,today=None,semester_starts=None):
    today=today or datetime.now(timezone(timedelta(hours=8))).date()
    academic_year=today.year if today.month>=9 else today.year-1
    number=1 if today.month>=9 or today.month<=2 else (2 if today.month<=6 else 3)
    current=(academic_year,number)
    dated=[]
    known_terms=set()
    for label,start in (semester_starts or {}).items():
        parts=term_parts(label)
        try:
            day=date.fromisoformat(start[:10])
        except (ValueError,TypeError):
            continue
        if parts:
            known_terms.add(parts)
            if day<=today:
                dated.append((day,parts))
    if dated:
        latest=max(dated)[1]
        # Actual school dates cross the September calendar boundary. If the
        # expected term has a date, use the latest started term, including its
        # predecessor while waiting. Do not promote stale old-only datasets.
        if current in known_terms or latest>current:
            current=latest
    grouped={}
    for course in courses:
        label=course.get('semester') or '未标注学期'
        if label not in grouped:
            grouped[label]={'key':label,'label':label,'is_current':term_parts(label)==current,
                            'courses':[],'totals':{k:0 for k in COUNTS}}
        g=grouped[label];g['courses'].append(course)
        for k in COUNTS:
            g['totals'][k]+=course.get(k,0) or 0
    return sorted(grouped.values(),key=lambda g:(g['is_current'],term_parts(g['label']) or (0,0),g['label']),reverse=True)
