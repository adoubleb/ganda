# Transaction that filters an embedded link list and deletes a dropped link's target fails at COMMIT

**OrientDB version:** 3.2.57 (official Docker image `orientdb:3.2.57`; also seen on 3.2.56)
**Java version:** OpenJDK 1.8.0_504
**OS:** Ubuntu 26.04 (container)
**Found by:** the fuzzer's write IR: the doc side's cascade of a node delete (drop the links pointing at the victims, then delete them) failed only when the side drew the transactional spelling.

## Steps to reproduce

In the OrientDB console, connected to an empty database (`script sql` ... `end` runs a server-side SQL script, which the remote console needs for `BEGIN` / `COMMIT`):

```sql
CREATE CLASS d;

-- d0 holds an embedded list of two links, to d1 and d2
script sql
LET $a = INSERT INTO d SET id = 'd1';
LET $b = INSERT INTO d SET id = 'd2';
INSERT INTO d SET id = 'd0', links = [{"target": $a[0].@rid}, {"target": $b[0].@rid}];
end

-- in one transaction: drop d0's link to d1, then delete d1
script sql
BEGIN;
UPDATE d SET links = links[target.id <> 'd1'] WHERE id = 'd0';
DELETE FROM d WHERE id = 'd1';
COMMIT;
end
-- COMMIT fails: Cannot save (1) document {target:#26:0}: no class or cluster defined; the transaction is rolled back
-- (Wrong: expected the commit to succeed, leaving d0 with the link to d2 only and d1 deleted)
-- the same UPDATE and DELETE without BEGIN / COMMIT succeed
```

## Expected behavior

The transaction commits: `d0` keeps only the link to `d2`, and `d1` is deleted.

## Actual behavior

`COMMIT` fails and the transaction is rolled back:

```
Error on saving record #-1:-1
-> Cannot save (1) document {target:#26:0}: no class or cluster defined
```

The same `UPDATE` and `DELETE` run without `BEGIN` / `COMMIT` succeed and give the expected result.

## Why this is a bug

The two statements are valid and succeed in auto-commit mode, but wrapped in a transaction they cannot be committed: the commit fails with an internal save error and all of the transaction's work is lost.
