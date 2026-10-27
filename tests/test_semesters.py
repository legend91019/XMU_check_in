from datetime import date

from xmu.semesters import group_courses


def test_current_term_first_and_other_terms_descending():
    courses=[
        {'id':'old','semester':'2025-2026第1学期','present':9,'absent':3,'total':12},
        {'id':'summer','semester':'2025-2026第3学期','present':2,'total':2},
        {'id':'current','semester':'2026-2027第1学期','present':4,'absent':0,'total':4},
    ]
    groups=group_courses(courses,date(2026,10,26))
    assert [g['courses'][0]['id'] for g in groups]==['current','summer','old']
    assert groups[0]['is_current'] is True
    assert groups[0]['totals']['absent']==0
    assert groups[1]['is_current'] is False


def test_january_belongs_to_previous_autumn():
    groups=group_courses([{'semester':'2026-2027第1学期'}],date(2027,1,10))
    assert groups[0]['is_current'] is True


def test_missing_current_semester_not_replaced_by_latest_old_semester():
    groups=group_courses([{'semester':'2025-2026第2学期'}],date(2026,10,26))
    assert not any(g['is_current'] for g in groups)


def test_unknown_semester_kept_in_separate_group():
    groups=group_courses([{'semester':''},{'semester':'2026-2027第1学期'}],date(2026,10,26))
    assert groups[-1]['label']=='未标注学期'
    assert len(groups[-1]['courses'])==1


def test_school_start_dates_override_calendar_at_semester_boundary():
    courses=[{'semester':'2025-2026第2学期'},{'semester':'2025-2026第3学期'}]
    groups=group_courses(courses,date(2026,6,30),{'2025-2026第2学期':'2026-03-02','2025-2026第3学期':'2026-06-29'})
    assert groups[0]['label']=='2025-2026第3学期'
    assert groups[0]['is_current'] is True


def test_autumn_that_has_not_started_does_not_replace_current_summer():
    cs=[{'semester':'2025-2026第3学期'},{'semester':'2026-2027第1学期'}]
    starts={'2025-2026第3学期':'2026-06-29','2026-2027第1学期':'2026-09-14'}
    groups=group_courses(cs,date(2026,9,5),starts)
    assert groups[0]['label']=='2025-2026第3学期' and groups[0]['is_current']


def test_autumn_starting_in_august_overrides_calendar_year_cutoff():
    cs=[{'semester':'2025-2026第3学期'},{'semester':'2026-2027第1学期'}]
    starts={'2025-2026第3学期':'2026-06-29','2026-2027第1学期':'2026-08-24'}
    groups=group_courses(cs,date(2026,8,26),starts)
    assert groups[0]['label']=='2026-2027第1学期' and groups[0]['is_current']
