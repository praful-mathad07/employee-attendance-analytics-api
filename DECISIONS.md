# Design Decisions

1. **Indexes.** A unique index on `employees.emp_code` enforces the employee business identifier. A unique compound index on `(emp_code, date)` prevents duplicate attendance records for one employee/day. A date index supports date-range attendance queries. The unique attendance index is essential; application-level pre-checks alone cannot prevent concurrent duplicates. A larger compound index can be added after measuring real query plans.

2. **Punch-in race.** The handler checks for an existing record for a clear 409 response, but the unique compound index is the final concurrency guard. If two requests race, one insert succeeds and the other receives `DuplicateKeyError`, which is converted to HTTP 409.

3. **Ties.** The late leaderboard uses MongoDB ranking so employees tied on total late minutes share a rank. Filtering by rank rather than taking only the first `limit` documents preserves ties at the cutoff.

4. **Headcount.** Department headcount is computed from the employees collection using `joined_on` through the reporting period end, independently of attendance logs. Departments with employees but no logs are still returned with zero attendance metrics.

5. **Scaling.** For 100x the data, I would benchmark the actual aggregation pipelines with representative data and query plans, then add only indexes supported by those measurements and automate regression tests for the R1–R10 edge cases.
