"""Fixed cabinet scope and the manual capacity/power allocation policy."""

STATION_CABINETS = {
    'station-1': ('ES01', ('emu11', 'emu12')),
    'station-2': ('ES02', ('emu21', 'emu22', 'emu23', 'emu24', 'emu25', 'emu26')),
}
CABINET_ISOLATION_POLICY = 'configured-equal-capacity-power-v1'
