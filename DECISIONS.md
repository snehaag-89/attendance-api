# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees (no duplicate employees). Unique (`emp_code`, `date`) on logs: one record per day, PATCH lookups, monthly summary. (`date` desc, `emp_code`): attendance list sort and leaderboard date range. (`emp_code`, `punch_in` desc): finding the record to punch out. (`department`, `joined_on`) and (`joined_on`): headcount. Rejected: an index on `status`; it has five values and would only slow writes.
2. **Punch-in race.** Both requests build the same (`emp_code`, `date`) record and call `insert_one`. The unique index lets MongoDB accept one and reject the other with `DuplicateKeyError`, which I return as 409. I never read first, so there is no gap.
3. **Ties.** I use `$rank` (1, 2, 2, 4) and keep every row with `rank <= limit`. Tied people share a rank, so all are returned, even if that is more than `limit` rows; dropping one would be arbitrary.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s each person's logs. Someone with no logs has an empty array but is still counted by `$group`.
5. **At 100x data.** I would keep daily roll-up documents per employee, updated on punch and PATCH, so analytics don't scan raw logs.
