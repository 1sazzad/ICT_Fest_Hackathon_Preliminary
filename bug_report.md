# Bug Report

## 1) Offset-aware datetime inputs were not converted to UTC
- **Business rule:** 1 (Datetimes)
- **Location:** `app/timeutils.py` → `parse_input_datetime`
- **Bug:** Timezone-aware inputs were stripped with `replace(tzinfo=None)` without UTC conversion.
- **Observable impact:** Offset inputs were stored/compared as wrong instants; API responses returned incorrect UTC times.
- **Fix:** Convert aware datetimes with `astimezone(timezone.utc)` before dropping `tzinfo`.

## 2) Notification lock-order inversion could deadlock
- **Business rule:** 16 (Liveness)
- **Location:** `app/services/notifications.py` → `notify_created`, `notify_cancelled`
- **Bug:** Different lock acquisition orders between create/cancel notification paths.
- **Observable impact:** Concurrent valid requests could deadlock.
- **Fix:** Use the same lock order in both paths.

## 3) Overlap predicate incorrectly rejected back-to-back bookings
- **Business rule:** 3 (No double booking)
- **Location:** `app/routers/bookings.py` → `_has_conflict`
- **Bug:** Inclusive overlap (`<=`) treated touching intervals as conflicts.
- **Observable impact:** Back-to-back bookings were incorrectly rejected with `ROOM_CONFLICT`.
- **Fix:** Use strict predicate: `existing.start < new.end AND new.start < existing.end`.

## 4) Refund tier boundaries were incorrect
- **Business rule:** 6 (Cancellation/refund)
- **Location:** `app/routers/bookings.py` → `cancel_booking`
- **Bug:** Tier logic gave wrong percentages at boundary and for `<24h` notice.
- **Observable impact:** Incorrect refund percent for valid cancellations.
- **Fix:** Implement exact tiers: `>=48h -> 100%`, `>=24h and <48h -> 50%`, `<24h -> 0%`.

## 5) Access token lifetime used wrong unit
- **Business rule:** 8 (Auth)
- **Location:** `app/auth.py` → `create_access_token`
- **Bug:** Lifetime multiplied minutes by 60 inside `timedelta(minutes=...)`.
- **Observable impact:** Access tokens lived much longer than 900 seconds.
- **Fix:** Use `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.

## 6) Logout revocation checked wrong claim
- **Business rule:** 8 (Auth)
- **Location:** `app/auth.py` → `get_token_payload`
- **Bug:** Revoked set stored `jti`, but validation checked `sub`.
- **Observable impact:** Logged-out access tokens could still be accepted.
- **Fix:** Validate revoked status by `jti`.

## 7) Admin export with room filter could bypass org scope
- **Business rule:** 9 (Multi-tenancy)
- **Location:** `app/services/export.py` → `generate_export`
- **Bug:** `include_all=True` + `room_id` used unscoped room fetch.
- **Observable impact:** Cross-org bookings could be exported by room id.
- **Fix:** Route all export queries through org-scoped fetch path.

## 8) Members could read other members’ bookings in same org
- **Business rule:** 10 (Booking visibility)
- **Location:** `app/routers/bookings.py` → `get_booking`
- **Bug:** Member ownership check was missing.
- **Observable impact:** Member could read another member’s booking instead of `404 BOOKING_NOT_FOUND`.
- **Fix:** Enforce owner-only read for members; admins keep org-wide read.

## 9) Pagination/order contract mismatches in booking list
- **Business rule:** 11 (Pagination)
- **Location:** `app/routers/bookings.py` → `list_bookings`
- **Bug:** Descending order, wrong offset formula, hardcoded limit.
- **Observable impact:** Skipped/reordered items and incorrect page size.
- **Fix:** Order by `start_time ASC, id ASC`, offset `(page-1)*limit`, apply requested `limit`.

## 10) Usage report cache was stale after booking creation
- **Business rule:** 12 (Usage report)
- **Location:** `app/routers/bookings.py` → `create_booking`
- **Bug:** Report cache invalidated on cancel only.
- **Observable impact:** `GET /admin/usage-report` could return stale data after creation.
- **Fix:** Invalidate org report cache after successful booking creation.

## 11) Availability cache was stale after cancellation
- **Business rule:** 13 (Availability)
- **Location:** `app/routers/bookings.py` → `cancel_booking`
- **Bug:** Availability cache invalidated on create but not cancel.
- **Observable impact:** Cancelled intervals could remain in cached busy slots.
- **Fix:** Invalidate room/date availability cache after cancellation.

## 12) Duplicate username registration returned success response
- **Business rule:** 15 (Registration)
- **Location:** `app/routers/auth.py` → `register`
- **Bug:** Existing user path returned a user payload.
- **Observable impact:** Duplicate username did not return required `409 USERNAME_TAKEN`.
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)`.

## 13) Booking window validation allowed invalid windows
- **Business rule:** 2 (Booking price/window)
- **Location:** `app/routers/bookings.py` → `create_booking`
- **Bug:** Allowed 5-minute grace into past; missed explicit `end > start`; no minimum 1-hour guard.
- **Observable impact:** Invalid booking windows could be accepted.
- **Fix:** Enforce `start > now`, `end > start`, whole-hour duration, and `1..8` hour range.

## 14) Single booking response could return wrong start_time
- **Business rule:** 1/contract correctness
- **Location:** `app/routers/bookings.py` → `get_booking`
- **Bug:** Response overwrote `start_time` with `created_at`.
- **Observable impact:** `GET /bookings/{id}` returned incorrect `start_time` value.
- **Fix:** Remove overwrite and keep serializer-provided `start_time`.

## 15) Refund rounding mode and stored/response value diverged
- **Business rule:** 6 (Cancellation/refund)
- **Location:** `app/routers/bookings.py`, `app/services/refunds.py`
- **Bug:** Response used float `round`, persisted value used float truncation.
- **Observable impact:** Half-cent behavior was wrong and response could differ from stored refund.
- **Fix:** Use integer half-up formula `(price_cents * refund_percent + 50) // 100`; persist and return the same exact integer.

## 16) Refresh tokens were reusable
- **Business rule:** 8 (Auth)
- **Location:** `app/auth.py`, `app/routers/auth.py`
- **Bug:** No consumed-refresh tracking.
- **Observable impact:** Same refresh token could be reused instead of returning 401.
- **Fix:** Track consumed refresh `jti` values and consume/check atomically under a lock.

## 17) Rate-limit bucket mutation was not thread-safe
- **Business rule:** 5 (Rate limit)
- **Location:** `app/services/ratelimit.py` → `record_and_check`
- **Bug:** Unsynchronized trim/append/store/check on shared per-user bucket.
- **Observable impact:** Concurrent requests could exceed 20-per-60s limit.
- **Fix:** Guard full mutation/check sequence with a lock.

## 18) Room stats endpoint used stale in-memory counters
- **Business rule:** 14 (Room stats)
- **Location:** `app/routers/rooms.py` → `room_stats`
- **Bug:** Endpoint returned in-memory counters not guaranteed to match DB state.
- **Observable impact:** Stats could diverge from actual confirmed bookings/revenue after bursts.
- **Fix:** Query DB directly for confirmed booking count and summed `price_cents` (with zero-safe sum).

## 19) Conflict check and booking commit were not serialized
- **Business rule:** 3 (No double booking under concurrency)
- **Location:** `app/routers/bookings.py` → `create_booking`
- **Bug:** Overlap check and insert/commit were separable across concurrent requests.
- **Observable impact:** Two overlapping confirmed bookings could both be created.
- **Fix:** Introduce one booking-state lock and perform conflict check + insert/commit inside one critical section.

## 20) Quota check and booking commit were not serialized
- **Business rule:** 4 (Quota under concurrency)
- **Location:** `app/routers/bookings.py` → `create_booking`
- **Bug:** Quota count and insert/commit were separable across concurrent requests.
- **Observable impact:** Concurrent creates could exceed the 3-booking quota window.
- **Fix:** Perform quota check and booking commit inside the same booking-state critical section.

## 21) Reference code generator was not thread-safe
- **Business rule:** 7 (Reference uniqueness)
- **Location:** `app/services/reference.py` → `next_reference_code`
- **Bug:** Counter increment had no lock.
- **Observable impact:** Concurrent create paths could produce duplicate reference candidates.
- **Fix:** Protect counter issuance with a lock.

## 22) Reference code uniqueness could collide with pre-existing rows
- **Business rule:** 7 (Reference uniqueness)
- **Location:** `app/routers/bookings.py` → `_next_unique_reference_code`, `create_booking`
- **Bug:** Freshly generated code was not checked against DB before use.
- **Observable impact:** After restarts/seeded data, generated code could collide with existing booking references.
- **Fix:** Loop until generated candidate is absent in `Booking` table before insert.

## 23) Booking model lacked uniqueness guard for reference_code
- **Business rule:** 7 (Reference uniqueness integrity)
- **Location:** `app/models.py` → `Booking.reference_code`
- **Bug:** No model-level uniqueness declaration.
- **Observable impact:** No schema-level guard when tables are created from current metadata.
- **Fix:** Added `unique=True` to `Booking.reference_code`.

## 24) Concurrent cancellation could create multiple refund logs
- **Business rule:** 6 (Exactly one RefundLog under concurrency)
- **Location:** `app/routers/bookings.py` → `cancel_booking`
- **Bug:** Status check and cancellation write were not serialized.
- **Observable impact:** Parallel cancels could both pass pre-check and both write refunds.
- **Fix:** Re-fetch/check status and perform refund+status mutation under booking-state lock.

## 25) Refund helper committed independently from cancellation status
- **Business rule:** 6 (Coherent cancellation transaction)
- **Location:** `app/services/refunds.py` → `log_refund`; `app/routers/bookings.py` → `cancel_booking`
- **Bug:** Refund row was committed separately from booking status update.
- **Observable impact:** Partial state was possible if later cancellation steps failed.
- **Fix:** `log_refund` now only stages row (`db.add`); router commits refund log and status together.

## 26) RefundLog model lacked uniqueness guard per booking
- **Business rule:** 6 (Refund integrity)
- **Location:** `app/models.py` → `RefundLog`
- **Bug:** No model-level uniqueness declaration on `booking_id`.
- **Observable impact:** No schema-level guard when tables are created from current metadata.
- **Fix:** Added unique constraint `uq_refund_booking_id` on `booking_id`.

## 27) Export with foreign/nonexistent room_id returned 200 instead of 404
- **Business rule:** 9 (Multi-tenancy)
- **Location:** `app/routers/admin.py` → `/admin/export`
- **Bug:** The endpoint accepted a `room_id` outside the caller org (or missing room) and returned CSV output.
- **Observable impact:** Cross-org/nonexistent resource IDs did not behave as non-existent on this code path.
- **Fix:** Added org-scoped `room_id` existence check before export generation; now returns `404 ROOM_NOT_FOUND` for foreign or missing rooms.

## 28) Booking quota was incorrectly enforced for admins
- **Business rule:** 4 (Booking quota)
- **Location:** `app/routers/bookings.py` → `create_booking`
- **Bug:** Quota check applied to all authenticated users, including admins.
- **Observable impact:** Admin users could be blocked with `409 QUOTA_EXCEEDED` even though Rule 4 limits quota to members.
- **Fix:** Apply `_check_quota(...)` only when `user.role == "member"`.

## 29) Malformed signed JWT claims could raise 500 instead of 401
- **Business rule:** 8 (Auth)
- **Location:** `app/auth.py` (`revoke_access_token`, `get_token_payload`, `get_current_user`), `app/routers/auth.py` (`refresh`)
- **Bug:** Missing/invalid claims (for example missing/non-integer `sub`, missing/invalid `jti`) could trigger unhandled `KeyError`/`TypeError`/`ValueError` paths.
- **Observable impact:** Some malformed-but-signed tokens produced server errors instead of contract-required unauthorized responses.
- **Fix:** Added defensive claim validation/parsing and return `401 UNAUTHORIZED` for malformed token payloads while preserving signature and expiry verification.
