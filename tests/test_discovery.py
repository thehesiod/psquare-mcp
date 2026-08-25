"""Tests for account discovery, including district-level accounts (#7 bug 2).

A district-level parent's root page reports gon.institute_type="District" and a
gon.institute_id that is a *district* id. That id is not a school: both
/schools/{id}/feeds and /api/v2/schools/{id} 404 for it. The old code used it
directly as current_school_id, so list_schools returned a placeholder
{school_id: <district_id>, name: "School <district_id>"} and every school-scoped
tool 404'd.

The member schools have to be resolved from the district switcher fragment
first. School accounts must keep their existing single-school behaviour.
"""

import pytest
from bs4 import BeautifulSoup

from parentsquare_mcp.client import PSClient

DISTRICT_ID = 900001
SCHOOL_A = 111
SCHOOL_B = 222


def _root(user_id=42, institute_id=DISTRICT_ID, institute_type="District"):
    type_line = f'gon.institute_type="{institute_type}";' if institute_type else ""
    return f"""
    <html><head><script>
      gon.user_id={user_id};gon.institute_id={institute_id};{type_line}
    </script></head><body></body></html>
    """


DISTRICT_FRAGMENT = f"""
<div class="district-schools">
  <a href="/schools/{SCHOOL_A}/feeds">Alpha Elementary</a>
  <a href="/schools/{SCHOOL_B}/feeds">Beta Middle</a>
</div>
"""

FEEDS_WITH_STUDENTS = """
<div class="sidebar">
  <a href="/students/501/dashboard">
    <h4>Ada Lovelace</h4>
    <div class="truncate-text">1st Grade • Alpha Elementary</div>
  </a>
  <a href="/students/502/dashboard">
    <h4>Alan Turing</h4>
    <div class="truncate-text">6th Grade • Beta Middle</div>
  </a>
</div>
"""


class FakeClient(PSClient):
    """PSClient with the network replaced by canned pages keyed on path."""

    def __init__(self, pages, json_pages=None, missing_json=()):
        super().__init__()
        self.pages = pages
        self.json_pages = json_pages or {}
        self.missing_json = set(missing_json)
        self.requested = []
        self.relogins = 0

    def get_page(self, path, params=None):
        self.requested.append(path)
        for key, html in self.pages.items():
            if path.startswith(key):
                return BeautifulSoup(html, "html.parser")
        raise AssertionError(f"unexpected page request: {path}")

    def get_json(self, path, params=None):
        self.requested.append(path)
        if path in self.missing_json:
            raise RuntimeError(f"404 for {path}")
        if path in self.json_pages:
            return self.json_pages[path]
        raise RuntimeError(f"404 for {path}")

    def _relogin(self):
        self.relogins += 1


def _school_json(name):
    return {"data": {"attributes": {"name": name}}}


def _district_client(**kw):
    return FakeClient(
        pages={
            "/layout_templates/district_switch_schools_list": DISTRICT_FRAGMENT,
            f"/schools/{SCHOOL_A}/feeds": FEEDS_WITH_STUDENTS,
            f"/schools/{SCHOOL_B}/feeds": FEEDS_WITH_STUDENTS,
            "/": _root(**kw),
        },
        json_pages={
            f"/api/v2/schools/{SCHOOL_A}": _school_json("Alpha Elementary"),
            f"/api/v2/schools/{SCHOOL_B}": _school_json("Beta Middle"),
        },
        # The district id is not a school; this endpoint 404s for it.
        missing_json=[f"/api/v2/schools/{DISTRICT_ID}"],
    )


def test_district_expands_into_member_schools():
    client = _district_client()

    account = client.discover_account()

    assert account.user_id == 42
    assert set(account.schools) == {SCHOOL_A, SCHOOL_B}


def test_district_id_is_never_treated_as_a_school():
    """The precise regression: the district id must not appear as a school."""
    client = _district_client()

    account = client.discover_account()

    assert DISTRICT_ID not in account.schools
    assert f"School {DISTRICT_ID}" not in account.schools.values()
    assert f"/schools/{DISTRICT_ID}/feeds" not in client.requested


def test_district_students_map_to_the_right_schools():
    client = _district_client()

    account = client.discover_account()

    assert account.students[501]["name"] == "Ada Lovelace"
    assert account.students[501]["school_id"] == SCHOOL_A
    assert account.students[502]["name"] == "Alan Turing"
    assert account.students[502]["school_id"] == SCHOOL_B


def test_district_school_names_prefer_the_api_over_anchor_text():
    client = _district_client()

    account = client.discover_account()

    assert account.schools[SCHOOL_A] == "Alpha Elementary"


def test_district_with_no_member_schools_returns_early():
    """Better an empty account than one built on a district id that 404s everywhere."""
    client = FakeClient(
        pages={
            "/layout_templates/district_switch_schools_list": "<div></div>",
            "/": _root(),
        },
        missing_json=[f"/api/v2/schools/{DISTRICT_ID}"],
    )

    account = client.discover_account()

    assert account.schools == {}
    assert f"/schools/{DISTRICT_ID}/feeds" not in client.requested


@pytest.mark.parametrize("institute_type", ["district", "District", "DISTRICT"])
def test_district_detection_is_case_insensitive(institute_type):
    client = _district_client(institute_type=institute_type)

    account = client.discover_account()

    assert set(account.schools) == {SCHOOL_A, SCHOOL_B}


def test_school_account_is_unchanged():
    """Non-district accounts must keep using institute_id directly."""
    client = FakeClient(
        pages={
            f"/schools/{SCHOOL_A}/feeds": FEEDS_WITH_STUDENTS,
            "/": _root(institute_id=SCHOOL_A, institute_type="School"),
        },
        json_pages={f"/api/v2/schools/{SCHOOL_A}": _school_json("Alpha Elementary")},
    )

    account = client.discover_account()

    assert set(account.schools) == {SCHOOL_A}
    assert "/layout_templates/district_switch_schools_list" not in client.requested


def test_missing_institute_type_behaves_as_a_school():
    """Older/other page shapes without gon.institute_type must not regress."""
    client = FakeClient(
        pages={
            f"/schools/{SCHOOL_A}/feeds": FEEDS_WITH_STUDENTS,
            "/": _root(institute_id=SCHOOL_A, institute_type=""),
        },
        json_pages={f"/api/v2/schools/{SCHOOL_A}": _school_json("Alpha Elementary")},
    )

    account = client.discover_account()

    assert set(account.schools) == {SCHOOL_A}


def test_unauthenticated_root_triggers_relogin():
    client = FakeClient(pages={"/": "<html><body>Sign In</body></html>"})

    client.discover_account()

    assert client.relogins == 1


def test_discovery_is_cached():
    client = _district_client()
    client.discover_account()
    before = len(client.requested)

    client.discover_account()

    assert len(client.requested) == before


class TestExtractGon:
    """gon.institute_type is the new field; pin the shapes it appears in."""

    @pytest.mark.parametrize(
        "script,expected",
        [
            ('gon.user_id=1;gon.institute_id=2;gon.institute_type="District";', "District"),
            ("gon.user_id=1;gon.institute_id=2;gon.institute_type='District';", "District"),
            ("gon.user_id=1;gon.institute_id=2;gon.institute_type = \"School\";", "School"),
            ("gon.user_id=1;gon.institute_id=2;", ""),
        ],
    )
    def test_institute_type_shapes(self, script, expected):
        client = FakeClient(pages={})
        soup = BeautifulSoup(f"<script>{script}</script>", "html.parser")

        _, _, institute_type = client._extract_gon(soup)

        assert institute_type == expected

    def test_ids_are_parsed(self):
        client = FakeClient(pages={})
        soup = BeautifulSoup(
            '<script>gon.user_id=42;gon.institute_id=900001;gon.institute_type="District";</script>',
            "html.parser",
        )

        user_id, institute_id, _ = client._extract_gon(soup)

        assert (user_id, institute_id) == (42, 900001)

    def test_no_gon_returns_zeros(self):
        client = FakeClient(pages={})
        soup = BeautifulSoup("<script>var x = 1;</script>", "html.parser")

        assert client._extract_gon(soup) == (0, 0, "")
