# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees, and unique (`emp_code`, `date`) on logs: they stop duplicates and serve per-employee queries. (`date` desc, `emp_code`) serves the attendance list and the month-range pipelines. (`status`, `date`, `emp_code`) serves the status filter. (`emp_code`, `punch_in`) finds the latest punch for a punch-out. (`department`, `joined_on`) serves headcount. I rejected an index on `late_minutes`: the date range already narrows the data, and it would only slow writes.

2. **Punch-in race.** Both requests build the record and call `insert_one` directly. MongoDB accepts one at the unique (`emp_code`, `date`) index; the other raises `DuplicateKeyError`, which I return as 409. There is no check before the insert.

3. **Ties.** The leaderboard uses `$rank`, so tied people share a rank. I keep every row with `rank <= limit`, so everyone tied at the cutoff is returned, even if that is more than `limit`.

4. **Headcount.** The summary starts from `employees`, not from logs, and looks up each person's logs. Someone with no logs still counts as one row.

5. **100x data.** I would keep monthly totals per employee in a separate collection, updated on each punch or correction, instead of re-reading raw logs on every request.
