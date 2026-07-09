from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app
from app.services import reference as reference_service

client = TestClient(app)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _iso_naive(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def _future(hours: float) -> datetime:
    return _utc_now() + timedelta(hours=hours)


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _assert_app_error(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["code"] == code, body


def _register(org: str, username: str, password: str = "pw12345") -> dict:
    r = client.post(
        "/auth/register",
        json={"org_name": org, "username": username, "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login(org: str, username: str, password: str = "pw12345") -> dict:
    r = client.post(
        "/auth/login",
        json={"org_name": org, "username": username, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _mk_org_admin() -> tuple[str, str, dict, dict[str, str]]:
    org = _uniq("org")
    username = _uniq("admin")
    reg = _register(org, username)
    assert reg["role"] == "admin"
    tokens = _login(org, username)
    return org, username, tokens, _auth_headers(tokens["access_token"])


def _mk_member(org: str) -> tuple[str, dict, dict[str, str]]:
    username = _uniq("member")
    reg = _register(org, username)
    assert reg["role"] == "member"
    tokens = _login(org, username)
    return username, tokens, _auth_headers(tokens["access_token"])


def _create_room(headers: dict[str, str], rate: int = 1000) -> int:
    r = client.post(
        "/rooms",
        json={"name": _uniq("room"), "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_booking(headers: dict[str, str], room_id: int, start_iso: str, end_iso: str):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start_iso, "end_time": end_iso},
        headers=headers,
    )


def _thread_post(path: str, *, json: dict | None, headers: dict[str, str], params: dict | None = None):
    with TestClient(app) as tc:
        return tc.post(path, json=json, headers=headers, params=params)


def test_rule_1_datetime_offset_and_naive_handling():
    _, _, _, headers = _mk_org_admin()
    room_id = _create_room(headers, rate=700)

    start_utc = _future(30).astimezone(timezone.utc).replace(microsecond=0)
    end_utc = start_utc + timedelta(hours=1)
    plus6 = timezone(timedelta(hours=6))

    resp_offset = _create_booking(
        headers,
        room_id,
        start_utc.astimezone(plus6).isoformat(),
        end_utc.astimezone(plus6).isoformat(),
    )
    assert resp_offset.status_code == 201, resp_offset.text
    body_offset = resp_offset.json()
    assert body_offset["start_time"] == _iso_utc(start_utc)
    assert body_offset["end_time"] == _iso_utc(end_utc)

    start_naive = _future(40).astimezone(timezone.utc).replace(microsecond=0)
    end_naive = start_naive + timedelta(hours=1)
    resp_naive = _create_booking(headers, room_id, _iso_naive(start_naive), _iso_naive(end_naive))
    assert resp_naive.status_code == 201, resp_naive.text
    body_naive = resp_naive.json()
    assert body_naive["start_time"] == _iso_utc(start_naive)
    assert body_naive["end_time"] == _iso_utc(end_naive)


def test_rule_2_booking_window_and_price():
    _, _, _, headers = _mk_org_admin()
    room_id = _create_room(headers, rate=1234)

    now = _utc_now().replace(microsecond=0)
    r = _create_booking(headers, room_id, _iso_utc(now - timedelta(hours=1)), _iso_utc(now + timedelta(hours=1)))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    r = _create_booking(headers, room_id, now.isoformat(), _iso_utc(now + timedelta(hours=1)))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    start = _future(30).replace(microsecond=0)
    r = _create_booking(headers, room_id, _iso_utc(start), _iso_utc(start))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    r = _create_booking(headers, room_id, _iso_utc(start), _iso_utc(start + timedelta(minutes=30)))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    r = _create_booking(headers, room_id, _iso_utc(start), _iso_utc(start + timedelta(hours=1, minutes=30)))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    r = _create_booking(headers, room_id, _iso_utc(start), _iso_utc(start + timedelta(hours=9)))
    _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    valid_start = _future(50).replace(microsecond=0)
    valid_end = valid_start + timedelta(hours=3)
    ok = _create_booking(headers, room_id, _iso_utc(valid_start), _iso_utc(valid_end))
    assert ok.status_code == 201, ok.text
    assert ok.json()["price_cents"] == 1234 * 3


def test_rule_3_conflict_overlap_back_to_back_and_concurrent_overlap():
    _, _, _, headers = _mk_org_admin()
    room_id = _create_room(headers, rate=1000)

    base_start = _future(60).replace(microsecond=0)
    base_end = base_start + timedelta(hours=2)
    first = _create_booking(headers, room_id, _iso_utc(base_start), _iso_utc(base_end))
    assert first.status_code == 201, first.text

    overlap = _create_booking(
        headers,
        room_id,
        _iso_utc(base_start + timedelta(hours=1)),
        _iso_utc(base_end + timedelta(hours=1)),
    )
    _assert_app_error(overlap, 409, "ROOM_CONFLICT")

    back_to_back = _create_booking(headers, room_id, _iso_utc(base_end), _iso_utc(base_end + timedelta(hours=1)))
    assert back_to_back.status_code == 201, back_to_back.text

    room2 = _create_room(headers, rate=1000)
    c_start = _future(80).replace(microsecond=0)
    payload = {
        "room_id": room2,
        "start_time": _iso_utc(c_start),
        "end_time": _iso_utc(c_start + timedelta(hours=2)),
    }

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_thread_post, "/bookings", json=payload, headers=headers) for _ in range(2)]
        results = [f.result(timeout=8) for f in futures]

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]
    assert [r for r in results if r.status_code == 409][0].json()["code"] == "ROOM_CONFLICT"


def test_rule_4_quota_sequential_and_concurrent():
    org, _, _, admin_headers = _mk_org_admin()
    _, _, member_headers = _mk_member(org)
    rooms = [_create_room(admin_headers, rate=500) for _ in range(5)]

    for i in range(3):
        s = _future(1 + i).replace(microsecond=0)
        r = _create_booking(member_headers, rooms[i], _iso_utc(s), _iso_utc(s + timedelta(hours=1)))
        assert r.status_code == 201, r.text

    fourth = _create_booking(member_headers, rooms[3], _iso_utc(_future(4).replace(microsecond=0)), _iso_utc(_future(5).replace(microsecond=0)))
    _assert_app_error(fourth, 409, "QUOTA_EXCEEDED")

    outside = _future(25).replace(microsecond=0)
    ok = _create_booking(member_headers, rooms[3], _iso_utc(outside), _iso_utc(outside + timedelta(hours=1)))
    assert ok.status_code == 201, ok.text

    org2, _, _, admin_headers2 = _mk_org_admin()
    _, _, member_headers2 = _mk_member(org2)
    rooms2 = [_create_room(admin_headers2, rate=500) for _ in range(5)]
    for i in range(2):
        s = _future(1 + i).replace(microsecond=0)
        r = _create_booking(member_headers2, rooms2[i], _iso_utc(s), _iso_utc(s + timedelta(hours=1)))
        assert r.status_code == 201, r.text

    payloads = []
    for i in range(3):
        s = _future(6 + i).replace(microsecond=0)
        payloads.append({"room_id": rooms2[2 + i], "start_time": _iso_utc(s), "end_time": _iso_utc(s + timedelta(hours=1))})

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(_thread_post, "/bookings", json=p, headers=member_headers2) for p in payloads]
        results = [f.result(timeout=10) for f in futures]

    success = [r for r in results if r.status_code == 201]
    quota_fail = [r for r in results if r.status_code == 409 and r.json().get("code") == "QUOTA_EXCEEDED"]
    assert len(success) == 1, [r.text for r in results]
    assert len(quota_fail) == 2, [r.text for r in results]


def test_rule_5_rate_limit_counts_failures_and_concurrency():
    _, _, _, headers = _mk_org_admin()
    room_id = _create_room(headers, rate=1000)

    past = _utc_now().replace(microsecond=0) - timedelta(hours=1)
    payload = {"room_id": room_id, "start_time": _iso_utc(past), "end_time": _iso_utc(past + timedelta(hours=1))}

    first_20 = [client.post("/bookings", json=payload, headers=headers) for _ in range(20)]
    for r in first_20:
        _assert_app_error(r, 400, "INVALID_BOOKING_WINDOW")

    twenty_first = client.post("/bookings", json=payload, headers=headers)
    _assert_app_error(twenty_first, 429, "RATE_LIMITED")

    _, _, _, headers2 = _mk_org_admin()
    room2 = _create_room(headers2, rate=1000)
    p2 = {
        "room_id": room2,
        "start_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=2)),
        "end_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=1)),
    }

    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = [ex.submit(_thread_post, "/bookings", json=p2, headers=headers2) for _ in range(25)]
        results = [f.result(timeout=12) for f in futures]

    non_429 = [r for r in results if r.status_code != 429]
    limited = [r for r in results if r.status_code == 429 and r.json().get("code") == "RATE_LIMITED"]
    assert len(non_429) <= 20
    assert len(limited) >= 5


def test_rule_6_cancellation_refunds_and_concurrent_cancel():
    _, _, _, headers = _mk_org_admin()
    room_id = _create_room(headers, rate=101)

    s100 = _future(49).replace(microsecond=0)
    b100 = _create_booking(headers, room_id, _iso_utc(s100), _iso_utc(s100 + timedelta(hours=1)))
    assert b100.status_code == 201
    id100 = b100.json()["id"]
    c100 = client.post(f"/bookings/{id100}/cancel", headers=headers)
    assert c100.status_code == 200
    assert c100.json()["refund_percent"] == 100
    assert c100.json()["refund_amount_cents"] == 101

    s50 = _future(30).replace(microsecond=0)
    b50 = _create_booking(headers, room_id, _iso_utc(s50), _iso_utc(s50 + timedelta(hours=1)))
    assert b50.status_code == 201
    id50 = b50.json()["id"]
    c50 = client.post(f"/bookings/{id50}/cancel", headers=headers)
    assert c50.status_code == 200
    assert c50.json()["refund_percent"] == 50
    assert c50.json()["refund_amount_cents"] == 51

    get50 = client.get(f"/bookings/{id50}", headers=headers)
    assert get50.status_code == 200
    refunds50 = get50.json()["refunds"]
    assert len(refunds50) == 1
    assert refunds50[0]["amount_cents"] == c50.json()["refund_amount_cents"]

    s0 = _future(10).replace(microsecond=0)
    b0 = _create_booking(headers, room_id, _iso_utc(s0), _iso_utc(s0 + timedelta(hours=1)))
    assert b0.status_code == 201
    id0 = b0.json()["id"]
    c0 = client.post(f"/bookings/{id0}/cancel", headers=headers)
    assert c0.status_code == 200
    assert c0.json()["refund_percent"] == 0
    assert c0.json()["refund_amount_cents"] == 0

    again = client.post(f"/bookings/{id0}/cancel", headers=headers)
    _assert_app_error(again, 409, "ALREADY_CANCELLED")

    s_conc = _future(35).replace(microsecond=0)
    bc = _create_booking(headers, room_id, _iso_utc(s_conc), _iso_utc(s_conc + timedelta(hours=1)))
    assert bc.status_code == 201
    bid = bc.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [
            ex.submit(_thread_post, f"/bookings/{bid}/cancel", json={}, headers=headers),
            ex.submit(_thread_post, f"/bookings/{bid}/cancel", json={}, headers=headers),
        ]
        results = [f.result(timeout=8) for f in futures]

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], [r.text for r in results]
    getc = client.get(f"/bookings/{bid}", headers=headers)
    assert getc.status_code == 200
    assert len(getc.json()["refunds"]) == 1


def test_rule_7_reference_codes_unique_sequential_and_concurrent():
    org, _, _, admin_headers = _mk_org_admin()
    member_headers = [_mk_member(org)[2] for _ in range(5)]
    rooms = [_create_room(admin_headers, rate=900) for _ in range(5)]

    refs_seq = []
    for i in range(3):
        s = _future(40 + i).replace(microsecond=0)
        r = _create_booking(member_headers[0], rooms[0], _iso_utc(s), _iso_utc(s + timedelta(hours=1)))
        assert r.status_code == 201, r.text
        refs_seq.append(r.json()["reference_code"])
    assert len(refs_seq) == len(set(refs_seq))

    start = _future(80).replace(microsecond=0)
    payloads = [
        {"room_id": rooms[i], "start_time": _iso_utc(start + timedelta(hours=i)), "end_time": _iso_utc(start + timedelta(hours=i + 1))}
        for i in range(5)
    ]

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [
            ex.submit(_thread_post, "/bookings", json=payloads[i], headers=member_headers[i])
            for i in range(5)
        ]
        results = [f.result(timeout=12) for f in futures]

    created = [r.json()["reference_code"] for r in results if r.status_code == 201]
    assert len(created) == 5, [r.text for r in results]
    assert len(created) == len(set(created))


def test_rule_8_auth_claims_expiry_logout_refresh_single_use_and_rotation():
    _, _, tokens, headers = _mk_org_admin()

    access_payload = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    refresh_payload = jwt.decode(tokens["refresh_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])

    for payload in (access_payload, refresh_payload):
        for claim in ("sub", "org", "role", "jti", "iat", "exp", "type"):
            assert claim in payload
        assert isinstance(payload["sub"], str)

    assert access_payload["type"] == "access"
    assert refresh_payload["type"] == "refresh"
    assert access_payload["exp"] - access_payload["iat"] == 900
    assert refresh_payload["exp"] - refresh_payload["iat"] == 7 * 24 * 3600

    before_logout = client.get("/bookings", headers=headers)
    assert before_logout.status_code == 200

    lo = client.post("/auth/logout", headers=headers)
    assert lo.status_code == 200

    after_logout = client.get("/bookings", headers=headers)
    _assert_app_error(after_logout, 401, "UNAUTHORIZED")

    r1 = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r1.status_code == 200, r1.text
    new_refresh = r1.json()["refresh_token"]

    reuse_old = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    _assert_app_error(reuse_old, 401, "UNAUTHORIZED")

    use_rotated = client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert use_rotated.status_code == 200, use_rotated.text


def test_rule_9_multi_tenancy_cross_org_ids_and_export_no_leakage():
    _, _, _, headers_a = _mk_org_admin()
    room_a = _create_room(headers_a, rate=1000)
    start = _future(50).replace(microsecond=0)
    b = _create_booking(headers_a, room_a, _iso_utc(start), _iso_utc(start + timedelta(hours=1)))
    assert b.status_code == 201
    booking_a = b.json()["id"]
    ref_a = b.json()["reference_code"]

    _, _, _, headers_b = _mk_org_admin()

    date = start.date().isoformat()
    av = client.get(f"/rooms/{room_a}/availability", params={"date": date}, headers=headers_b)
    _assert_app_error(av, 404, "ROOM_NOT_FOUND")

    stats = client.get(f"/rooms/{room_a}/stats", headers=headers_b)
    _assert_app_error(stats, 404, "ROOM_NOT_FOUND")

    gb = client.get(f"/bookings/{booking_a}", headers=headers_b)
    _assert_app_error(gb, 404, "BOOKING_NOT_FOUND")

    cb = client.post(f"/bookings/{booking_a}/cancel", headers=headers_b)
    _assert_app_error(cb, 404, "BOOKING_NOT_FOUND")

    exp = client.get("/admin/export", params={"room_id": room_a, "include_all": "true"}, headers=headers_b)
    _assert_app_error(exp, 404, "ROOM_NOT_FOUND")


def test_rule_10_booking_visibility_member_vs_admin():
    org, _, _, admin_headers = _mk_org_admin()
    _, _, member_a_headers = _mk_member(org)
    _, _, member_b_headers = _mk_member(org)

    room_id = _create_room(admin_headers, rate=1000)
    s = _future(55).replace(microsecond=0)
    b = _create_booking(member_b_headers, room_id, _iso_utc(s), _iso_utc(s + timedelta(hours=1)))
    assert b.status_code == 201
    bid = b.json()["id"]

    g = client.get(f"/bookings/{bid}", headers=member_a_headers)
    _assert_app_error(g, 404, "BOOKING_NOT_FOUND")

    c = client.post(f"/bookings/{bid}/cancel", headers=member_a_headers)
    _assert_app_error(c, 404, "BOOKING_NOT_FOUND")

    g_admin = client.get(f"/bookings/{bid}", headers=admin_headers)
    assert g_admin.status_code == 200

    c_admin = client.post(f"/bookings/{bid}/cancel", headers=admin_headers)
    assert c_admin.status_code == 200
    assert c_admin.json()["status"] == "cancelled"


def test_rule_11_pagination_and_ordering():
    _, _, _, headers = _mk_org_admin()
    room1 = _create_room(headers, rate=100)
    room2 = _create_room(headers, rate=100)

    created = []
    for i in range(12):
        s = _future(30 + i).replace(microsecond=0)
        r = _create_booking(headers, room1, _iso_utc(s), _iso_utc(s + timedelta(hours=1)))
        assert r.status_code == 201, r.text
        created.append(r.json())

    same_start = _future(100).replace(microsecond=0)
    r_a = _create_booking(headers, room1, _iso_utc(same_start), _iso_utc(same_start + timedelta(hours=1)))
    r_b = _create_booking(headers, room2, _iso_utc(same_start), _iso_utc(same_start + timedelta(hours=1)))
    assert r_a.status_code == 201 and r_b.status_code == 201

    full = client.get("/bookings", params={"page": 1, "limit": 100}, headers=headers)
    assert full.status_code == 200
    items = full.json()["items"]
    assert full.json()["total"] >= 14

    keys = [(it["start_time"], it["id"]) for it in items]
    assert keys == sorted(keys)

    default_page = client.get("/bookings", headers=headers)
    assert default_page.status_code == 200
    d = default_page.json()
    assert d["page"] == 1
    assert d["limit"] == 10
    assert len(d["items"]) == 10

    page1 = client.get("/bookings", params={"page": 1, "limit": 5}, headers=headers)
    page2 = client.get("/bookings", params={"page": 2, "limit": 5}, headers=headers)
    assert page1.status_code == 200 and page2.status_code == 200
    ids1 = [x["id"] for x in page1.json()["items"]]
    ids2 = [x["id"] for x in page2.json()["items"]]
    assert set(ids1).isdisjoint(ids2)


def test_rule_12_usage_report_inclusive_and_live_after_create_cancel():
    _, _, _, headers = _mk_org_admin()
    room_with = _create_room(headers, rate=300)
    room_zero = _create_room(headers, rate=300)

    start = _future(52).replace(microsecond=0)
    day = start.date().isoformat()

    before = client.get("/admin/usage-report", params={"from": day, "to": day}, headers=headers)
    assert before.status_code == 200
    rows_before = {r["room_id"]: r for r in before.json()["rooms"]}
    assert room_with in rows_before and room_zero in rows_before
    assert rows_before[room_with]["confirmed_bookings"] == 0
    assert rows_before[room_zero]["confirmed_bookings"] == 0

    b = _create_booking(headers, room_with, _iso_utc(start), _iso_utc(start + timedelta(hours=1)))
    assert b.status_code == 201
    bid = b.json()["id"]

    after_create = client.get("/admin/usage-report", params={"from": day, "to": day}, headers=headers)
    assert after_create.status_code == 200
    rows_create = {r["room_id"]: r for r in after_create.json()["rooms"]}
    assert rows_create[room_with]["confirmed_bookings"] == 1
    assert rows_create[room_with]["revenue_cents"] == 300
    assert rows_create[room_zero]["confirmed_bookings"] == 0

    cancel = client.post(f"/bookings/{bid}/cancel", headers=headers)
    assert cancel.status_code == 200

    after_cancel = client.get("/admin/usage-report", params={"from": day, "to": day}, headers=headers)
    assert after_cancel.status_code == 200
    rows_cancel = {r["room_id"]: r for r in after_cancel.json()["rooms"]}
    assert rows_cancel[room_with]["confirmed_bookings"] == 0
    assert rows_cancel[room_with]["revenue_cents"] == 0


def test_rule_13_availability_date_filter_order_and_live_updates():
    _, _, _, headers = _mk_org_admin()
    room = _create_room(headers, rate=400)

    day_start = (_utc_now() + timedelta(days=3)).replace(hour=8, minute=0, second=0, microsecond=0)
    day = day_start.date().isoformat()

    before = client.get(f"/rooms/{room}/availability", params={"date": day}, headers=headers)
    assert before.status_code == 200
    assert before.json()["busy"] == []

    s1 = day_start
    s2 = day_start + timedelta(hours=3)
    next_day = day_start + timedelta(days=1)

    b1 = _create_booking(headers, room, _iso_utc(s1), _iso_utc(s1 + timedelta(hours=1)))
    b2 = _create_booking(headers, room, _iso_utc(s2), _iso_utc(s2 + timedelta(hours=1)))
    b3 = _create_booking(headers, room, _iso_utc(next_day), _iso_utc(next_day + timedelta(hours=1)))
    assert b1.status_code == 201 and b2.status_code == 201 and b3.status_code == 201

    after_create = client.get(f"/rooms/{room}/availability", params={"date": day}, headers=headers)
    assert after_create.status_code == 200
    busy = after_create.json()["busy"]
    assert len(busy) == 2
    assert [x["start_time"] for x in busy] == sorted([x["start_time"] for x in busy])

    cancel_one = client.post(f"/bookings/{b1.json()['id']}/cancel", headers=headers)
    assert cancel_one.status_code == 200

    after_cancel = client.get(f"/rooms/{room}/availability", params={"date": day}, headers=headers)
    assert after_cancel.status_code == 200
    busy2 = after_cancel.json()["busy"]
    assert len(busy2) == 1
    assert busy2[0]["start_time"] == b2.json()["start_time"]


def test_rule_14_room_stats_live_matches_confirmed_bookings():
    _, _, _, headers = _mk_org_admin()
    room = _create_room(headers, rate=250)

    zero = client.get(f"/rooms/{room}/stats", headers=headers)
    assert zero.status_code == 200
    assert zero.json()["total_confirmed_bookings"] == 0
    assert zero.json()["total_revenue_cents"] == 0

    s1 = _future(70).replace(microsecond=0)
    s2 = _future(72).replace(microsecond=0)
    b1 = _create_booking(headers, room, _iso_utc(s1), _iso_utc(s1 + timedelta(hours=1)))
    b2 = _create_booking(headers, room, _iso_utc(s2), _iso_utc(s2 + timedelta(hours=2)))
    assert b1.status_code == 201 and b2.status_code == 201

    mid = client.get(f"/rooms/{room}/stats", headers=headers)
    assert mid.status_code == 200
    assert mid.json()["total_confirmed_bookings"] == 2
    assert mid.json()["total_revenue_cents"] == 250 + 500

    c = client.post(f"/bookings/{b1.json()['id']}/cancel", headers=headers)
    assert c.status_code == 200

    after = client.get(f"/rooms/{room}/stats", headers=headers)
    assert after.status_code == 200
    assert after.json()["total_confirmed_bookings"] == 1
    assert after.json()["total_revenue_cents"] == 500


def test_rule_15_registration_roles_and_duplicate_username():
    org = _uniq("org")
    username = _uniq("user")

    first = client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw12345"})
    assert first.status_code == 201
    assert first.json()["role"] == "admin"

    second = client.post("/auth/register", json={"org_name": org, "username": _uniq("user"), "password": "pw12345"})
    assert second.status_code == 201
    assert second.json()["role"] == "member"

    dup = client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw12345"})
    _assert_app_error(dup, 409, "USERNAME_TAKEN")


def test_rule_16_liveness_concurrent_create_cancel_completes_quickly():
    _, _, _, headers = _mk_org_admin()
    room = _create_room(headers, rate=100)

    existing_start = _future(90).replace(microsecond=0)
    existing = _create_booking(headers, room, _iso_utc(existing_start), _iso_utc(existing_start + timedelta(hours=1)))
    assert existing.status_code == 201
    booking_id = existing.json()["id"]

    create_payload = {
        "room_id": room,
        "start_time": _iso_utc(_future(95).replace(microsecond=0)),
        "end_time": _iso_utc(_future(96).replace(microsecond=0)),
    }

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_thread_post, "/bookings", json=create_payload, headers=headers)
        f2 = ex.submit(_thread_post, f"/bookings/{booking_id}/cancel", json={}, headers=headers)
        r1 = f1.result(timeout=8)
        r2 = f2.result(timeout=8)

    assert r1.status_code in (201, 400, 409, 429)
    assert r2.status_code in (200, 409)


def test_adv_case_1_cross_org_export_room_filter_is_404_for_foreign_and_missing():
    _, _, _, headers_a = _mk_org_admin()
    room_a = _create_room(headers_a)
    s = _future(60).replace(microsecond=0)
    assert _create_booking(headers_a, room_a, _iso_utc(s), _iso_utc(s + timedelta(hours=1))).status_code == 201

    _, _, _, headers_b = _mk_org_admin()

    for include_all in ("true", "false"):
        r = client.get("/admin/export", params={"room_id": room_a, "include_all": include_all}, headers=headers_b)
        _assert_app_error(r, 404, "ROOM_NOT_FOUND")

    missing_room_id = -1
    r_missing = client.get(
        "/admin/export",
        params={"room_id": missing_room_id, "include_all": "true"},
        headers=headers_b,
    )
    _assert_app_error(r_missing, 404, "ROOM_NOT_FOUND")


def test_adv_case_2_quota_applies_to_members_not_admins():
    _, _, _, admin_headers = _mk_org_admin()
    rooms = [_create_room(admin_headers) for _ in range(4)]

    for i in range(4):
        start = _future(1 + i).replace(microsecond=0)
        resp = _create_booking(admin_headers, rooms[i], _iso_utc(start), _iso_utc(start + timedelta(hours=1)))
        assert resp.status_code == 201, resp.text


def test_adv_case_3_cancelled_state_not_counted_for_conflict_or_quota_or_reports():
    org, _, _, admin_headers = _mk_org_admin()
    _, _, member_headers = _mk_member(org)

    rooms = [_create_room(admin_headers) for _ in range(5)]

    conflict_start = _future(30).replace(minute=0, second=0, microsecond=0)
    b1 = _create_booking(member_headers, rooms[0], _iso_utc(conflict_start), _iso_utc(conflict_start + timedelta(hours=1)))
    assert b1.status_code == 201, b1.text
    assert client.post(f"/bookings/{b1.json()['id']}/cancel", headers=member_headers).status_code == 200

    same_slot = _create_booking(member_headers, rooms[0], _iso_utc(conflict_start), _iso_utc(conflict_start + timedelta(hours=1)))
    assert same_slot.status_code == 201, same_slot.text

    q1 = _future(2).replace(minute=0, second=0, microsecond=0)
    q2 = _future(3).replace(minute=0, second=0, microsecond=0)
    q3 = _future(4).replace(minute=0, second=0, microsecond=0)

    bq1 = _create_booking(member_headers, rooms[1], _iso_utc(q1), _iso_utc(q1 + timedelta(hours=1)))
    bq2 = _create_booking(member_headers, rooms[2], _iso_utc(q2), _iso_utc(q2 + timedelta(hours=1)))
    bq3 = _create_booking(member_headers, rooms[3], _iso_utc(q3), _iso_utc(q3 + timedelta(hours=1)))
    assert bq1.status_code == 201 and bq2.status_code == 201 and bq3.status_code == 201

    assert client.post(f"/bookings/{bq2.json()['id']}/cancel", headers=member_headers).status_code == 200

    q4 = _future(5).replace(minute=0, second=0, microsecond=0)
    bq4 = _create_booking(member_headers, rooms[4], _iso_utc(q4), _iso_utc(q4 + timedelta(hours=1)))
    assert bq4.status_code == 201, bq4.text

    day = q2.date().isoformat()
    av = client.get(f"/rooms/{rooms[2]}/availability", params={"date": day}, headers=member_headers)
    assert av.status_code == 200
    assert av.json()["busy"] == []

    stats = client.get(f"/rooms/{rooms[2]}/stats", headers=member_headers)
    assert stats.status_code == 200
    assert stats.json()["total_confirmed_bookings"] == 0
    assert stats.json()["total_revenue_cents"] == 0

    usage = client.get("/admin/usage-report", params={"from": day, "to": day}, headers=admin_headers)
    assert usage.status_code == 200
    rows = {r["room_id"]: r for r in usage.json()["rooms"]}
    assert rows[rooms[2]]["confirmed_bookings"] == 0


def test_adv_case_4_rate_limit_boundary_isolation_and_concurrent_counting():
    _, _, _, headers_a = _mk_org_admin()
    room_a = _create_room(headers_a)
    past = _utc_now().replace(microsecond=0) - timedelta(hours=1)
    payload_a = {"room_id": room_a, "start_time": _iso_utc(past), "end_time": _iso_utc(past + timedelta(hours=1))}

    first_20 = [client.post("/bookings", json=payload_a, headers=headers_a) for _ in range(20)]
    assert all(r.status_code == 400 and r.json().get("code") == "INVALID_BOOKING_WINDOW" for r in first_20)

    r21 = client.post("/bookings", json=payload_a, headers=headers_a)
    _assert_app_error(r21, 429, "RATE_LIMITED")

    _, _, _, headers_b = _mk_org_admin()
    room_b = _create_room(headers_b)
    payload_b = {
        "room_id": room_b,
        "start_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=2)),
        "end_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=1)),
    }
    rb = client.post("/bookings", json=payload_b, headers=headers_b)
    _assert_app_error(rb, 400, "INVALID_BOOKING_WINDOW")

    _, _, _, headers_c = _mk_org_admin()
    room_c = _create_room(headers_c)
    payload_c = {
        "room_id": room_c,
        "start_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=3)),
        "end_time": _iso_utc(_utc_now().replace(microsecond=0) - timedelta(hours=2)),
    }
    with ThreadPoolExecutor(max_workers=21) as ex:
        futures = [ex.submit(_thread_post, "/bookings", json=payload_c, headers=headers_c) for _ in range(21)]
        results = [f.result(timeout=12) for f in futures]

    limited = [r for r in results if r.status_code == 429 and r.json().get("code") == "RATE_LIMITED"]
    invalid = [r for r in results if r.status_code == 400 and r.json().get("code") == "INVALID_BOOKING_WINDOW"]
    assert len(limited) == 1, [r.text for r in results]
    assert len(invalid) == 20, [r.text for r in results]


def test_adv_case_5_concurrent_refresh_single_use_rotation_kept_valid():
    _, _, tokens, _ = _mk_org_admin()
    payload = {"refresh_token": tokens["refresh_token"]}

    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_thread_post, "/auth/refresh", json=payload, headers={}) for _ in range(2)]
        results = [f.result(timeout=8) for f in futures]

    ok = [r for r in results if r.status_code == 200]
    denied = [r for r in results if r.status_code == 401 and r.json().get("code") == "UNAUTHORIZED"]
    assert len(ok) == 1, [r.text for r in results]
    assert len(denied) == 1, [r.text for r in results]

    rotated = ok[0].json()["refresh_token"]
    use_rotated = client.post("/auth/refresh", json={"refresh_token": rotated})
    assert use_rotated.status_code == 200, use_rotated.text

    reuse_old = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    _assert_app_error(reuse_old, 401, "UNAUTHORIZED")


def test_adv_case_6_logout_corners_isolated_and_refresh_not_bearer():
    _, _, tokens_a, headers_a = _mk_org_admin()
    _, _, tokens_b, headers_b = _mk_org_admin()

    before = client.get("/bookings", headers=headers_a)
    assert before.status_code == 200

    logout = client.post("/auth/logout", headers=headers_a)
    assert logout.status_code == 200

    after = client.get("/bookings", headers=headers_a)
    _assert_app_error(after, 401, "UNAUTHORIZED")

    logout_again = client.post("/auth/logout", headers=headers_a)
    _assert_app_error(logout_again, 401, "UNAUTHORIZED")

    other_user_still_ok = client.get("/bookings", headers=headers_b)
    assert other_user_still_ok.status_code == 200

    refresh_as_bearer = client.get("/bookings", headers=_auth_headers(tokens_b["refresh_token"]))
    _assert_app_error(refresh_as_bearer, 401, "UNAUTHORIZED")


def test_adv_case_7_malformed_jwt_claims_return_401_not_500():
    _, _, tokens, _ = _mk_org_admin()
    access_payload = jwt.decode(tokens["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    refresh_payload = jwt.decode(tokens["refresh_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])

    now_ts = int(_utc_now().timestamp())

    def sign(payload: dict) -> str:
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    access_cases = []

    p_missing_sub = dict(access_payload)
    p_missing_sub.pop("sub", None)
    p_missing_sub["jti"] = uuid4().hex
    access_cases.append(p_missing_sub)

    p_non_int_sub = dict(access_payload)
    p_non_int_sub["sub"] = "abc"
    p_non_int_sub["jti"] = uuid4().hex
    access_cases.append(p_non_int_sub)

    p_missing_jti = dict(access_payload)
    p_missing_jti.pop("jti", None)
    access_cases.append(p_missing_jti)

    p_missing_type = dict(access_payload)
    p_missing_type.pop("type", None)
    p_missing_type["jti"] = uuid4().hex
    access_cases.append(p_missing_type)

    p_wrong_type = dict(access_payload)
    p_wrong_type["type"] = "refresh"
    p_wrong_type["jti"] = uuid4().hex
    access_cases.append(p_wrong_type)

    p_expired = dict(access_payload)
    p_expired["exp"] = now_ts - 1
    p_expired["jti"] = uuid4().hex
    access_cases.append(p_expired)

    for payload in access_cases:
        r = client.get("/bookings", headers=_auth_headers(sign(payload)))
        _assert_app_error(r, 401, "UNAUTHORIZED")

    refresh_cases = []

    r_missing_sub = dict(refresh_payload)
    r_missing_sub.pop("sub", None)
    r_missing_sub["jti"] = uuid4().hex
    refresh_cases.append(r_missing_sub)

    r_non_int_sub = dict(refresh_payload)
    r_non_int_sub["sub"] = "abc"
    r_non_int_sub["jti"] = uuid4().hex
    refresh_cases.append(r_non_int_sub)

    r_missing_jti = dict(refresh_payload)
    r_missing_jti.pop("jti", None)
    refresh_cases.append(r_missing_jti)

    r_wrong_type = dict(refresh_payload)
    r_wrong_type["type"] = "access"
    r_wrong_type["jti"] = uuid4().hex
    refresh_cases.append(r_wrong_type)

    r_expired = dict(refresh_payload)
    r_expired["exp"] = now_ts - 1
    r_expired["jti"] = uuid4().hex
    refresh_cases.append(r_expired)

    for payload in refresh_cases:
        r = client.post("/auth/refresh", json={"refresh_token": sign(payload)})
        _assert_app_error(r, 401, "UNAUTHORIZED")


def test_adv_case_8_datetime_offset_corners_use_utc_for_storage_and_reports():
    _, _, _, headers = _mk_org_admin()
    room_main = _create_room(headers)
    room_prev = _create_room(headers)
    room_next = _create_room(headers)

    base = (_utc_now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)

    z_start = base
    z_end = z_start + timedelta(hours=1)
    rz = _create_booking(headers, room_main, z_start.isoformat().replace("+00:00", "Z"), z_end.isoformat().replace("+00:00", "Z"))
    assert rz.status_code == 201
    assert rz.json()["start_time"] == _iso_utc(z_start)

    plus6 = timezone(timedelta(hours=6))
    minus5 = timezone(timedelta(hours=-5))

    p6_start = base + timedelta(hours=2)
    p6_end = p6_start + timedelta(hours=1)
    rp6 = _create_booking(headers, room_main, p6_start.astimezone(plus6).isoformat(), p6_end.astimezone(plus6).isoformat())
    assert rp6.status_code == 201
    assert rp6.json()["start_time"] == _iso_utc(p6_start)

    m5_start = base + timedelta(hours=4)
    m5_end = m5_start + timedelta(hours=1)
    rm5 = _create_booking(headers, room_main, m5_start.astimezone(minus5).isoformat(), m5_end.astimezone(minus5).isoformat())
    assert rm5.status_code == 201
    assert rm5.json()["start_time"] == _iso_utc(m5_start)

    mixed_start = base + timedelta(hours=6)
    mixed_end = mixed_start + timedelta(hours=2)
    rmixed = _create_booking(
        headers,
        room_main,
        mixed_start.astimezone(plus6).isoformat(),
        mixed_end.astimezone(minus5).isoformat(),
    )
    assert rmixed.status_code == 201
    assert rmixed.json()["start_time"] == _iso_utc(mixed_start)
    assert rmixed.json()["end_time"] == _iso_utc(mixed_end)

    prev_utc = (_utc_now() + timedelta(days=4)).replace(hour=19, minute=0, second=0, microsecond=0)
    prev_local_start = prev_utc.astimezone(plus6)
    prev_local_end = (prev_utc + timedelta(hours=1)).astimezone(plus6)
    rprev = _create_booking(headers, room_prev, prev_local_start.isoformat(), prev_local_end.isoformat())
    assert rprev.status_code == 201
    assert rprev.json()["start_time"] == _iso_utc(prev_utc)

    next_utc = (_utc_now() + timedelta(days=4)).replace(hour=2, minute=0, second=0, microsecond=0)
    if next_utc <= _utc_now():
        next_utc = next_utc + timedelta(days=1)
    next_local_start = next_utc.astimezone(minus5)
    next_local_end = (next_utc + timedelta(hours=1)).astimezone(minus5)
    rnext = _create_booking(headers, room_next, next_local_start.isoformat(), next_local_end.isoformat())
    assert rnext.status_code == 201
    assert rnext.json()["start_time"] == _iso_utc(next_utc)

    prev_utc_day = prev_utc.date().isoformat()
    prev_local_day = prev_local_start.date().isoformat()

    av_utc = client.get(f"/rooms/{room_prev}/availability", params={"date": prev_utc_day}, headers=headers)
    assert av_utc.status_code == 200
    assert [x["start_time"] for x in av_utc.json()["busy"]] == [_iso_utc(prev_utc)]

    if prev_local_day != prev_utc_day:
        av_local = client.get(f"/rooms/{room_prev}/availability", params={"date": prev_local_day}, headers=headers)
        assert av_local.status_code == 200
        assert av_local.json()["busy"] == []

    report_utc = client.get("/admin/usage-report", params={"from": prev_utc_day, "to": prev_utc_day}, headers=headers)
    assert report_utc.status_code == 200
    rows_utc = {r["room_id"]: r for r in report_utc.json()["rooms"]}
    assert rows_utc[room_prev]["confirmed_bookings"] == 1

    if prev_local_day != prev_utc_day:
        report_local = client.get("/admin/usage-report", params={"from": prev_local_day, "to": prev_local_day}, headers=headers)
        assert report_local.status_code == 200
        rows_local = {r["room_id"]: r for r in report_local.json()["rooms"]}
        assert rows_local[room_prev]["confirmed_bookings"] == 0


def test_adv_case_9_pagination_tie_order_no_skip_no_repeat_across_boundary():
    _, _, _, headers = _mk_org_admin()
    rooms = [_create_room(headers) for _ in range(6)]
    tie_start = _future(40).replace(minute=0, second=0, microsecond=0)

    created = []
    for room_id in rooms:
        r = _create_booking(headers, room_id, _iso_utc(tie_start), _iso_utc(tie_start + timedelta(hours=1)))
        assert r.status_code == 201, r.text
        created.append(r.json())

    expected_ids = sorted(x["id"] for x in created)

    p1 = client.get("/bookings", params={"page": 1, "limit": 3}, headers=headers)
    p2 = client.get("/bookings", params={"page": 2, "limit": 3}, headers=headers)
    assert p1.status_code == 200 and p2.status_code == 200

    ids = [x["id"] for x in p1.json()["items"] + p2.json()["items"]]
    assert ids == expected_ids
    assert len(ids) == len(set(ids))


def test_adv_case_10_cross_org_tenant_matrix_404_with_defined_codes():
    _, _, _, headers_a = _mk_org_admin()
    room_a = _create_room(headers_a)
    start = _future(48).replace(microsecond=0)
    b = _create_booking(headers_a, room_a, _iso_utc(start), _iso_utc(start + timedelta(hours=1)))
    assert b.status_code == 201
    booking_a = b.json()["id"]

    _, _, _, headers_b = _mk_org_admin()

    av = client.get(f"/rooms/{room_a}/availability", params={"date": start.date().isoformat()}, headers=headers_b)
    _assert_app_error(av, 404, "ROOM_NOT_FOUND")

    st = client.get(f"/rooms/{room_a}/stats", headers=headers_b)
    _assert_app_error(st, 404, "ROOM_NOT_FOUND")

    create_foreign = _create_booking(headers_b, room_a, _iso_utc(_future(50).replace(microsecond=0)), _iso_utc(_future(51).replace(microsecond=0)))
    _assert_app_error(create_foreign, 404, "ROOM_NOT_FOUND")

    exp = client.get("/admin/export", params={"room_id": room_a, "include_all": "true"}, headers=headers_b)
    _assert_app_error(exp, 404, "ROOM_NOT_FOUND")

    gb = client.get(f"/bookings/{booking_a}", headers=headers_b)
    _assert_app_error(gb, 404, "BOOKING_NOT_FOUND")

    cb = client.post(f"/bookings/{booking_a}/cancel", headers=headers_b)
    _assert_app_error(cb, 404, "BOOKING_NOT_FOUND")


def test_adv_case_11_reference_counter_restart_like_collision_still_unique():
    _, _, _, headers = _mk_org_admin()
    room = _create_room(headers)

    s1 = _future(60).replace(microsecond=0)
    s2 = _future(61).replace(microsecond=0)
    b1 = _create_booking(headers, room, _iso_utc(s1), _iso_utc(s1 + timedelta(hours=1)))
    b2 = _create_booking(headers, room, _iso_utc(s2), _iso_utc(s2 + timedelta(hours=1)))
    assert b1.status_code == 201 and b2.status_code == 201

    refs = {b1.json()["reference_code"], b2.json()["reference_code"]}
    max_num = max(int(r.split("-")[1]) for r in refs)

    with reference_service._counter_lock:
        reference_service._counter["value"] = max_num

    s3 = _future(62).replace(microsecond=0)
    b3 = _create_booking(headers, room, _iso_utc(s3), _iso_utc(s3 + timedelta(hours=1)))
    assert b3.status_code == 201, b3.text
    assert b3.json()["reference_code"] not in refs


def test_adv_case_12_exact_response_contract_shapes():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    org = _uniq("contract-org")
    username = _uniq("contract-user")

    reg = client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw12345"})
    assert reg.status_code == 201
    assert set(reg.json().keys()) == {"user_id", "org_id", "username", "role"}

    login = client.post("/auth/login", json={"org_name": org, "username": username, "password": "pw12345"})
    assert login.status_code == 200
    assert set(login.json().keys()) == {"access_token", "refresh_token", "token_type"}
    assert login.json()["token_type"] == "bearer"

    headers = _auth_headers(login.json()["access_token"])

    refresh = client.post("/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert refresh.status_code == 200
    assert set(refresh.json().keys()) == {"access_token", "refresh_token", "token_type"}
    assert refresh.json()["token_type"] == "bearer"

    room = client.post(
        "/rooms",
        json={"name": _uniq("contract-room"), "capacity": 4, "hourly_rate_cents": 1000},
        headers=headers,
    )
    assert room.status_code == 201
    assert set(room.json().keys()) == {"id", "org_id", "name", "capacity", "hourly_rate_cents"}
    room_id = room.json()["id"]

    rooms = client.get("/rooms", headers=headers)
    assert rooms.status_code == 200
    assert isinstance(rooms.json(), list)
    assert set(rooms.json()[0].keys()) == {"id", "org_id", "name", "capacity", "hourly_rate_cents"}

    start = _future(70).replace(microsecond=0)
    booking = _create_booking(headers, room_id, _iso_utc(start), _iso_utc(start + timedelta(hours=2)))
    assert booking.status_code == 201
    booking_keys = {"id", "reference_code", "room_id", "user_id", "start_time", "end_time", "status", "price_cents", "created_at"}
    assert set(booking.json().keys()) == booking_keys
    booking_id = booking.json()["id"]

    availability = client.get(f"/rooms/{room_id}/availability", params={"date": start.date().isoformat()}, headers=headers)
    assert availability.status_code == 200
    assert set(availability.json().keys()) == {"room_id", "date", "busy"}
    assert set(availability.json()["busy"][0].keys()) == {"start_time", "end_time"}

    stats = client.get(f"/rooms/{room_id}/stats", headers=headers)
    assert stats.status_code == 200
    assert set(stats.json().keys()) == {"room_id", "total_confirmed_bookings", "total_revenue_cents"}

    listing = client.get("/bookings", headers=headers)
    assert listing.status_code == 200
    assert set(listing.json().keys()) == {"items", "page", "limit", "total"}

    single = client.get(f"/bookings/{booking_id}", headers=headers)
    assert single.status_code == 200
    assert set(single.json().keys()) == booking_keys | {"refunds"}
    assert isinstance(single.json()["refunds"], list)

    usage = client.get(
        "/admin/usage-report",
        params={"from": start.date().isoformat(), "to": start.date().isoformat()},
        headers=headers,
    )
    assert usage.status_code == 200
    assert set(usage.json().keys()) == {"from", "to", "rooms"}
    assert {"room_id", "room_name", "confirmed_bookings", "revenue_cents"}.issubset(set(usage.json()["rooms"][0].keys()))

    export = client.get("/admin/export", headers=headers)
    assert export.status_code == 200
    assert export.text.splitlines()[0] == "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"

    cancel = client.post(f"/bookings/{booking_id}/cancel", headers=headers)
    assert cancel.status_code == 200
    assert set(cancel.json().keys()) == {"id", "status", "refund_percent", "refund_amount_cents"}

    logout = client.post("/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json() == {"status": "ok"}
