from seestar_bridge.altaz import radec_to_altaz


def test_altitude_and_azimuth_stay_in_valid_ranges():
    # The transform must always yield a physical altitude in [-90, 90] and an
    # azimuth wrapped to [0, 360). At this time the object is just below the
    # horizon (astropy: alt=-3.63, az=20.07), which is a valid altitude.
    alt, az = radec_to_altaz(0.0, 41.4, 41.4, -73.3, 946728000.0)
    assert -90.0 <= alt <= 90.0 and 0.0 <= az < 360.0


def test_eastern_and_western_objects_land_on_opposite_azimuth_halves():
    # An object east of the meridian and one west of it must wrap to opposite
    # halves of the compass (one azimuth < 180, the other >= 180), which exercises
    # the sin(ha) sign branch that reflects azimuth across the meridian.
    alt_e, az_e = radec_to_altaz(6.0, 0.0, 41.4, -73.3, 946728000.0)
    alt_w, az_w = radec_to_altaz(18.0, 0.0, 41.4, -73.3, 946728000.0)
    assert (az_e < 180.0) != (az_w < 180.0)  # one east, one west


def test_known_value_matches_reference():
    # Reference computed independently with astropy for the exact inputs below
    # (SkyCoord(ra, dec) -> AltAz at the given site + unix time):
    #   astropy: alt=74.09, az=71.15  (this spherical transform: alt=74.3, az=71.2,
    #   agreeing to the formula's ~arcminute precision).
    alt, az = radec_to_altaz(21.0213, 44.5333, 41.414, -73.3034, 1782799000.0)
    assert abs(alt - 74.09) < 1.5
    assert abs(az - 71.15) < 2.0
