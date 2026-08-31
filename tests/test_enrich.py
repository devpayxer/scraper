from ebayparts.enrich import enrich_title, parse_part_category, parse_vehicle, parse_years


class TestYears:
    def test_range(self):
        assert parse_years("2000-2003 CHEVY TAHOE WHEEL") == (2000, 2003)

    def test_single(self):
        assert parse_years("2018 Honda Accord Steering Wheel") == (2018, 2018)

    def test_two_digit_range(self):
        assert parse_years("07-13 BMW 328i TAIL LIGHT") == (2007, 2013)

    def test_none(self):
        assert parse_years("Universal Chrome Trim Piece") == (None, None)

    def test_tire_size_is_not_a_year(self):
        assert parse_years("WHEEL RIM 245/75 R16") == (None, None)


class TestVehicle:
    def test_leftmost_make_wins(self):
        make, model, _ = parse_vehicle("2013-2019 Toyota 86/BRZ/FRS Radiator Tank")
        assert (make, model) == ("Toyota", "86")

    def test_tire_model_is_not_mistaken_for_a_car(self):
        # "PATHFINDER" here is the tire, not a Nissan. Models are only ever
        # searched inside the winning make's own list.
        make, model, _ = parse_vehicle(
            "2000-2003 CHEVY TAHOE WHEEL RIM TIRE PATHFINDER HT 245/75 R16"
        )
        assert (make, model) == ("Chevrolet", "Tahoe")

    def test_chassis_code_resolves_to_model(self):
        make, model, _ = parse_vehicle("2014-2017 MERCEDES S-CLASS S550 W222 SEDAN MIRROR")
        assert (make, model) == ("Mercedes-Benz", "S-Class")

    def test_longest_alias_wins(self):
        make, model, _ = parse_vehicle("2016 Jeep Grand Cherokee ABS Pump")
        assert (make, model) == ("Jeep", "Grand Cherokee")

    def test_no_vehicle(self):
        assert parse_vehicle("Random Widget") == (None, None, [])


class TestPartCategory:
    def test_reservoir_beats_radiator(self):
        assert parse_part_category(
            "Radiator Coolant Overflow Reservoir Tank Unit"
        )[0] == "Coolant Reservoir"

    def test_wheel_beats_tire(self):
        assert parse_part_category("WHEEL RIM TIRE 245/75 R16")[0] == "Wheel / Rim"

    def test_interior_mirror_beats_side_mirror(self):
        assert parse_part_category("VIEW INTERIOR MIRROR AUTO DIM")[0] == "Interior Mirror"

    def test_steering_wheel_is_not_a_rim(self):
        assert parse_part_category("Steering Wheel Leather Black")[0] == "Steering Wheel"

    def test_wheel_bearing_is_not_a_rim(self):
        assert parse_part_category("Front Wheel Hub Bearing Assembly")[0] == "Wheel Hub / Bearing"

    def test_group_is_returned(self):
        assert parse_part_category("LEFT HEADLIGHT HALOGEN") == ("Headlight", "Lighting")

    def test_unmatched(self):
        assert parse_part_category("Mystery Object") == (None, None)


class TestEnrichTitle:
    def test_full_row(self):
        e = enrich_title("2015-2018 FORD F-150 DRIVER LEFT SIDE HEADLIGHT HALOGEN OEM")
        assert e.year_range == "2015-2018"
        assert e.vehicle == "Ford F-150"
        assert e.part_category == "Headlight"
        assert e.is_oem is True
        assert e.side == "Left"

    def test_ambiguous_side_is_left_blank(self):
        e = enrich_title("Left and Right Headlight Pair OEM")
        assert e.side is None
