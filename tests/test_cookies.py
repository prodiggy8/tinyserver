from cookies import parse_cookie_header, is_valid_chatname, build_set_cookie


def test_parse_cookie_header_single_pair():
    assert parse_cookie_header("chatname=quietfalcon42") == {"chatname": "quietfalcon42"}


def test_parse_cookie_header_multiple_pairs():
    assert parse_cookie_header("a=1; chatname=quietfalcon42; b=2") == {
        "a": "1",
        "chatname": "quietfalcon42",
        "b": "2",
    }


def test_parse_cookie_header_empty_and_none():
    assert parse_cookie_header("") == {}
    assert parse_cookie_header(None) == {}


def test_parse_cookie_header_skips_malformed_fragments():
    assert parse_cookie_header("a=1; garbage; =novalue; b=2") == {"a": "1", "b": "2"}


def test_parse_cookie_header_value_containing_equals():
    assert parse_cookie_header("token=abc=def") == {"token": "abc=def"}


def test_is_valid_chatname_accepts_well_formed():
    assert is_valid_chatname("quietfalcon42") is True
    assert is_valid_chatname("a00") is True


def test_is_valid_chatname_rejects_bad_format():
    assert is_valid_chatname("Quietfalcon42") is False  # uppercase
    assert is_valid_chatname("quietfalcon4") is False  # 1 digit
    assert is_valid_chatname("quietfalcon420") is False  # 3 digits
    assert is_valid_chatname("quietfalcon") is False  # no digits
    assert is_valid_chatname("42quietfalcon") is False  # digits first
    assert is_valid_chatname("quiet falcon42") is False  # space
    assert is_valid_chatname("") is False
    assert is_valid_chatname(None) is False


def test_is_valid_chatname_enforces_max_length():
    ok = "a" * 30 + "42"  # 32 chars total
    assert len(ok) == 32
    assert is_valid_chatname(ok) is True

    too_long = "a" * 31 + "42"  # 33 chars total
    assert len(too_long) == 33
    assert is_valid_chatname(too_long) is False


def test_build_set_cookie_default_attrs():
    value = build_set_cookie("chatname", "quietfalcon42")
    assert value == "chatname=quietfalcon42; Path=/; Max-Age=31536000; SameSite=Lax"


def test_build_set_cookie_custom_attrs():
    value = build_set_cookie("chatname", "x", path="/chat", max_age=60, same_site="Strict")
    assert value == "chatname=x; Path=/chat; Max-Age=60; SameSite=Strict"
