import math

R = 6371000.0


def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return R * 2 * math.asin(min(1, math.sqrt(h)))


def candidates(a, b, ra, rb):
    """APK-compatible local projection; impossible distances produce no candidates."""
    if not all(math.isfinite(x) and x >= 0 for x in (ra, rb)):
        return []
    lat0, lon0 = (a[0]+b[0])/2, (a[1]+b[1])/2
    def project(p):
        return (R*math.cos(math.radians(lat0))*math.radians(p[1]-lon0), R*math.radians(p[0]-lat0))
    ax, ay = project(a); bx, by = project(b)
    dx, dy = bx-ax, by-ay
    d = math.hypot(dx, dy)
    if not d or d > ra+rb or d < abs(ra-rb):
        return []
    along = (ra*ra-rb*rb+d*d)/(2*d)
    h2 = ra*ra-along*along
    if h2 < 0:
        return []
    h = math.sqrt(h2)
    x, y = ax+along*dx/d, ay+along*dy/d
    return [(math.degrees((y+s*h*dx/d)/R)+lat0,
             math.degrees((x-s*h*dy/d)/(R*math.cos(math.radians(lat0))))+lon0) for s in (1, -1)]
