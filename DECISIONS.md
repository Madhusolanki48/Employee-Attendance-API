# DECISIONS.md

1. **Indexes.** `employees.emp_code` (unique) enforces identity and serves lookups/joins. `employees(department, joined_on)` serves the trend and department filters; `employees.joined_on` serves the summary's headcount match. `attendance_logs(emp_code, date)` (unique) guarantees one record per day and serves per-employee queries and the `$lookup`s. `attendance_logs(date desc, emp_code)` serves the list sort and month ranges. I rejected an index on `status`: it has only five values, so it filters poorly.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. I never check first. The unique index lets MongoDB accept exactly one insert; the other raises `DuplicateKeyError`, which I turn into 409.

3. **Ties.** I rank with `$setWindowFields` + `$rank` (1, 2, 2, 4) and filter `rank <= limit` afterwards, so everyone tied at the cutoff is returned, even if that is more than `limit` rows. Ties are ordered by `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and `$lookup`s their logs, so an employee with no logs is still one row with zero totals.

5. **100x data.** Pre-aggregate daily/monthly totals per department when records are written, instead of scanning raw logs on every request.
