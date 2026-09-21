from backend.app.recipients.parser import is_valid_email, make_batches, parse_csv, parse_text, to_text


def test_email_validation():
    good = ["john@example.com", "first.last+tag@sub.example.co.uk", "x_y-z@ex-ample.io", "o'neil@example.org"]
    bad = ["", "plainaddress", "@example.com", "john@", "john@example", "john..doe@example.com",
           ".john@example.com", "john@-example.com", "john@exa mple.com", "a@b@c.com", "john@example.c"]
    assert all(is_valid_email(e) for e in good)
    assert not any(is_valid_email(e) for e in bad)


def test_separators_and_normalization():
    text = " John@Example.com,mary@example.com; a@example.com  b@example.com\n\n\tc@example.com \n mailto:D@Example.com "
    result = parse_text(text)
    emails = [r.email for r in result.valid]
    assert emails == ["john@example.com", "mary@example.com", "a@example.com", "b@example.com",
                      "c@example.com", "d@example.com"]
    assert result.invalid == []


def test_duplicates_removed_case_insensitive():
    result = parse_text("a@example.com\nA@EXAMPLE.COM\nb@example.com, a@example.com")
    assert [r.email for r in result.valid] == ["a@example.com", "b@example.com"]
    assert result.stats() == {"total": 4, "valid": 2, "invalid": 0, "duplicates": 2, "ready": 2, "named": 0}


def test_invalid_and_empty_values():
    result = parse_text("good@example.com,, ;bad@, @nope.com,,\n\n   \nalso-bad")
    assert [r.email for r in result.valid] == ["good@example.com"]
    assert result.invalid == ["bad@", "@nope.com", "also-bad"]
    assert result.stats()["total"] == 4


def test_named_formats():
    result = parse_text('John Doe <john@example.com>\nMary Smith, mary@example.com\n"Bob" <bob@example.com>')
    assert [(r.name, r.email) for r in result.valid] == [
        ("John Doe", "john@example.com"), ("Mary Smith", "mary@example.com"), ("Bob", "bob@example.com")]


def test_csv_email_column():
    data = b"email\njohn@example.com\nmary@example.com\nsales@example.com\n"
    result = parse_csv(data)
    assert [r.email for r in result.valid] == ["john@example.com", "mary@example.com", "sales@example.com"]


def test_csv_name_email_with_header_and_bom():
    data = "﻿name,email\nJohn Doe,john@example.com\nMary Smith,mary@example.com\n,\nBad Row,not-an-email\n".encode()
    result = parse_csv(data)
    assert [(r.name, r.email) for r in result.valid] == [("John Doe", "john@example.com"), ("Mary Smith", "mary@example.com")]
    assert result.invalid == ["not-an-email"]


def test_csv_headerless_name_email_and_semicolons():
    result = parse_csv(b"John Doe;john@example.com\nMary Smith;mary@example.com\nMary Again;MARY@example.com\n")
    assert [(r.name, r.email) for r in result.valid] == [("John Doe", "john@example.com"), ("Mary Smith", "mary@example.com")]
    assert result.duplicates == ["mary@example.com"]


def test_csv_extra_columns():
    data = b"First Name,Last Name,Email,Company\nJohn,Doe,john@example.com,ACME\n"
    result = parse_csv(data)
    assert [(r.name, r.email) for r in result.valid] == [("John Doe", "john@example.com")]


def test_csv_round_trip_through_text():
    result = parse_csv(b"name,email\nJohn Doe,john@example.com\nplain,p@example.com\n")
    again = parse_text(to_text(result.valid))
    assert [(r.name, r.email) for r in again.valid] == [(r.name, r.email) for r in result.valid]


def test_name_cannot_inject_header_characters():
    result = parse_csv(b'name,email\n"Evil\r\nBcc: x@evil.com <>",evil@example.com\n')
    assert "\n" not in result.valid[0].name and "<" not in result.valid[0].name


def test_bcc_batching():
    items = list(range(1000))
    batches = make_batches(items, 100)
    assert len(batches) == 10 and all(len(b) == 100 for b in batches)
    uneven = make_batches(list(range(250)), 100)
    assert [len(b) for b in uneven] == [100, 100, 50]
    assert sum(uneven, []) == list(range(250))
