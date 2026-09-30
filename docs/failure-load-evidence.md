# Failure and load evidence

These measurements use private temporary directories, an isolated synthetic HTTP API, a fresh SQLite identity database, and owned subprocesses on ephemeral loopback ports. They never send real email, reset the working UID database, start containers, or create cloud resources.

From the repository root, repeat the larger profile:

```sh
cd local
../.venv/bin/python -m scripts.failure_load_test --clients 50 --messages 1000 --rounds 3 --output artifacts/failure-load-evidence.json
```

For the independently maintained AWS application copy, use the same command from `aws/`. Its synthetic runner explicitly selects SQLite, local TCP, and a disabled readiness port. It exercises the AWS application package, but does **not** test PostgreSQL, the NLB, Fargate, or distributed capacity.

## Recorded results

| Package/profile | Clients | Inbox messages | FETCH/NOOP commands | Total duration | Client p95 duration | Errors | Server RSS after load |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Local | 50 | 1,003 | 450 | 6.937 s | 6.918 s | 0 | 139,888 KiB |
| AWS package with SQLite | 20 | 103 | 180 | 1.130 s | 1.129 s | 0 | 79,904 KiB |

Machine: macOS, Python 3.12.14. The synthetic upstream allows 1,000 requests/second; production configuration defaults to 5 requests/second per credential. Each client logs in, opens INBOX, then repeats complete metadata FETCH, three raw PEEK messages, and NOOP three times. All clients start together. Total duration includes login and selection; client durations include the startup barrier. RSS is one sample after completion, **not peak memory**. The evidence JSON files contain the detailed results. No inference about real AgentMail throughput, maximum connections, or AWS sizing follows from these numbers.

## Failure checks

- Three injected HTTP 503 responses exhaust the bounded refresh retry policy. NOOP emits a retained-view ALERT; the next refresh succeeds with identical UIDs.
- Six raw HTTP 429 responses exceed the ordinary retry count. FETCH keeps retrying and eventually returns all three complete literals.
- Process restart preserves UIDVALIDITY and message-to-UID mappings.
- Invalid credentials are rejected while valid sessions remain usable.
- A client disconnects during a long raw retry. A new session can refresh successfully.
- With four connection slots occupied, the fifth connection receives BYE. Closing the four clients allows a new authenticated connection.

Focused automated tests also check keepalive socket options, literal-safe heartbeat boundaries, idle timeout cleanup, short-lived cache bounds, transient/permanent failures, pagination, exact bytes, and read-state propagation. AWS coordination tests check lease renewal failure, cancellation cleanup, coalescing, and snapshot invalidation using mocks. Real PostgreSQL fencing/takeover tests remain opt-in and are explicitly skipped without `IMAP_TEST_POSTGRES_DSN`.

## Keepalives

Accepted sockets enable `SO_KEEPALIVE`: idle 60 seconds, probe interval 20 seconds, three probes where the operating system supports those options. Idle sessions receive `* OK Keepalive` every 120 seconds. The response shares the session writer lock and is suppressed during commands, so it cannot split a message literal. Transport probes cover long silent commands; application heartbeats cover otherwise idle clients. The existing 1,800-second client command inactivity limit still closes unused sessions; server heartbeats do not reset that timer.

Configure both copies with `IMAP_HEARTBEAT_INTERVAL_SECONDS`, `IMAP_TCP_KEEPALIVE_IDLE`, `IMAP_TCP_KEEPALIVE_INTERVAL`, and `IMAP_TCP_KEEPALIVE_COUNT`. Platform-specific tuning failures are logged and do not crash a connection. For hosted TLS, keep the timers below the NLB's fixed 350-second TLS idle timeout. [AWS documents the timeout and TCP keepalive handling](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html).

## Remaining evidence before hosted acceptance

Build the Linux image; run against a disposable PostgreSQL database; then test two or more application workers sharing that database, process termination during scans and STORE, loss of database connectivity, revoked credentials, slow readers, NLB idle connections, RDS failover, and autoscaling/draining under sustained traffic. Measure peak CPU/RSS, per-command percentiles, upstream 429 rates, pool occupancy, active connections, and reconnection behavior. Existing IMAP sessions stay on their original task; scaling only distributes new connections. CPU/memory policies can miss a workload that exhausts connection slots while mostly idle; a connection-utilization metric/policy remains a possible improvement.
