# LIKE wildcards do not match line breaks

**OrientDB version:** 3.2.56 (official Docker image `orientdb:3.2.56`)
**Java version:** OpenJDK 1.8.0_502
**OS:** Ubuntu 26.04 (container)

## Steps to reproduce

```sql
CREATE CLASS l;
CREATE PROPERTY l.s STRING;
INSERT INTO l SET s = 'ab';
INSERT INTO l SET s = 'a\tb';
INSERT INTO l SET s = 'a\nb';
INSERT INTO l SET s = 'a\rb';

SELECT s FROM l WHERE s LIKE 'a%';
SELECT s FROM l WHERE s LIKE '%b';
SELECT s FROM l WHERE s LIKE '%a%';
SELECT s FROM l WHERE s LIKE 'a?b';
```

## Expected behavior

`%` matches any sequence of characters, so the first three queries return all four rows. `?` matches any single character, so the last query returns `a\tb`, `a\nb` and `a\rb`.

## Actual behavior

Each query returns only the rows without a line break: `ab` and `a\tb` for the first three, and `a\tb` for the last. The rows holding `\n` or `\r` are never matched. `` behaves the same.

## Analysis

The LIKE pattern seems to be turned into a Java regular expression (`%` -> `.*`, `?` -> `.`) compiled without `Pattern.DOTALL`, so `.` does not match line terminators (`\n`, `\r`, `\u0085`, ``, ``). Compiling with `DOTALL` would give `%` and `?` their SQL meaning.

## Report Status
Reported as 10895. Confirmed.