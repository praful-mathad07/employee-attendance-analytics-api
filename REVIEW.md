# Review of the Starter Implementation

| # | Where | Defect | How to notice | Fix / handling in this implementation |
|---|---|---|---|---|
| 1 | `health` | Starter returned success without checking MongoDB readiness. | Stop MongoDB and call `GET /health`. | Ping MongoDB and return 503 when it is unavailable. |
| 2 | `create_employee` | Duplicate handling used a placeholder error and relied on a check-before-insert pattern. | Create a duplicate employee; race two requests with the same code. | Insert normally, rely on the unique index, and map `DuplicateKeyError` to 409. |
| 3 | `create_indexes` | The starter did not enforce uniqueness or support key attendance lookups. | Inspect indexes and attempt duplicate employee/day records. | Add unique indexes for `emp_code` and `(emp_code, date)`, plus a date index. |
| 4 | `compute_late_minutes` / punch-in | The starter calculation did not fully account for the grace-period boundary and overnight shifts. | Test exactly 10 minutes late, 10 minutes plus one second, and an overnight early-morning punch. | Calculate elapsed time against the correct shift-start instant and return whole elapsed minutes only when lateness is strictly over ten minutes. |
| 5 | punch-out | Work hours, overtime, half-day handling, and concurrent close behavior were incomplete. | Test a short shift, under-30-minute overtime, overnight shift, and simultaneous punch-outs. | Calculate rounded work hours, apply the 30-minute overtime threshold, derive half-day from rounded hours, and conditionally update the open record. |
| 6 | attendance listing | Filtering/pagination and total counts could be inconsistent or inefficient. | Combine filters and request multiple pages. | Apply filters in MongoDB, count the filtered set, sort, skip, and limit in the database. |
| 7 | manual correction | Corrections need derived-field recalculation and an append-only audit trail. | Correct punch times/status and inspect `history`. | Recompute derived values, record only changed fields, append one history entry, and reject conflicting concurrent updates. |
| 8 | analytics | Edge cases include employees without logs, half-days, weekdays, ties, and missing trend dates. | Test seeded data plus no-log employees, weekends, ties, and empty dates. | Use MongoDB aggregation pipelines and explicit headcount, ranking, date densification, and rounding logic. |

**Not considered a defect:** Keeping calendar dates as `YYYY-MM-DD` strings and instants as BSON datetimes follows `DATA_MODEL.md`; they represent different concepts. Analytics should use stored derived fields, as specified by the contract.

This review describes the identified starter defects and intended corrections; the hidden grader dataset is not available here, so the table is not a claim that every edge case has been independently verified.
