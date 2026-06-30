"""RA/Dec -> Alt/Az conversion (pure math, no I/O)."""
import math


def radec_to_altaz(
    ra_hours: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
    unix_t: float,
) -> tuple[float, float]:
    """Convert RA (hours) / Dec (deg) to Alt/Az (deg) for the given site and time.

    Standard spherical transform; longitude is east-positive (the scope reports
    western longitudes as negative, matching this convention). Azimuth is measured
    from North (0=N, 90=E). Good to ~arcminutes, which is plenty for a dashboard.

    Returns ``(altitude_deg, azimuth_deg)`` rounded to 0.1 deg.
    """
    jd = unix_t / 86400.0 + 2440587.5
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0
    ha = math.radians((gmst + lon_deg - ra_hours * 15.0) % 360.0)
    dec, lat = math.radians(dec_deg), math.radians(lat_deg)
    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / (math.cos(alt) * math.cos(lat))
    az = math.acos(max(-1.0, min(1.0, cos_az)))
    if math.sin(ha) > 0:
        az = 2 * math.pi - az
    return round(math.degrees(alt), 1), round(math.degrees(az), 1)
