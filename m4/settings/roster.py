"""Project cabinet scope and homogeneous capacity/power allocation policy."""

from shared.project import get_project

STATION_CABINETS = {s.id: (s.source_code, s.cabinet_sns) for s in get_project().stations}
CABINET_ISOLATION_POLICY = 'configured-equal-capacity-power-v1'
